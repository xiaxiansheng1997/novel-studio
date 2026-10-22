"""页面模式对齐的两条判据：没配控件且目标为关不算失败；绑错控件不许去点页面。

前端每轮请求都会带上 thinking/search 的布尔值（关的时候是 false），于是有两类噪音/风险：

1. 千问这类页面本来就没有这两个开关。若把 want=False 也记 missing，管理页与日志
   会每轮报「未对齐」，真正的配置缺失反而被淹没。
2. 更糟的是「配了但配错」：把模型下拉的文案（如 Qwen3.7-千问）当成了思考开关。
   想开想关都会拿这串字去页面上找按钮，命中的就是模型下拉触发器 ——
   点一下把菜单打开，等于每轮请求都在动页面。这种必须识别出来并一律不点。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.browser import ui_modes
from app.browser.ui_modes import _misbind_hint, apply_mode
from app.config import UiControlConfig
from app.core.modes import ModePlan

# 千问实测的误绑：思考/联网 都填了模型下拉里的那串文案
MODEL_TEXT = "Qwen3.7-千问"


class _FakePage:
    """只够读 url；任何 DOM 操作都会抛异常（对齐逻辑不该碰页面）。"""

    url = "https://chat.qwen.ai/c/x"

    def __getattr__(self, name):  # noqa: ANN001 - 兜住所有未实现的方法
        raise AttributeError(name)


def _cfg(ui: dict | None = None, models: list | None = None) -> SimpleNamespace:
    # ui 为空：等价于千问这种「页面没有思考/联网开关」的 provider
    return SimpleNamespace(ui=ui or {}, models=models or [])


def _qwen_cfg() -> SimpleNamespace:
    return _cfg(
        ui={"thinking": UiControlConfig(labels=[MODEL_TEXT]), "search": UiControlConfig(labels=[MODEL_TEXT])},
        models=[SimpleNamespace(label=MODEL_TEXT)],
    )


@pytest.mark.asyncio
async def test_missing_control_off_is_not_a_failure() -> None:
    plan = ModePlan(preset_id="quick", features={"thinking": False, "search": False})
    res = await apply_mode(_FakePage(), "qwen", plan, _cfg())
    assert res["ok"] is True
    assert res["missing"] == []
    # 但不能谎称“页面无此开关”：如实记进 skipped，供 /live 面板与日志排查
    assert res["skipped"] == ["thinking", "search"]
    assert all(d.get("skipped") for d in res["details"])


@pytest.mark.asyncio
async def test_missing_control_on_still_reports() -> None:
    plan = ModePlan(preset_id="think", features={"thinking": True})
    res = await apply_mode(_FakePage(), "qwen", plan, _cfg())
    assert res["ok"] is False
    assert res["missing"] == ["thinking"]


def test_misbind_hint_recognizes_model_text_and_duplicates() -> None:
    ui = {"thinking": UiControlConfig(labels=[MODEL_TEXT]), "search": UiControlConfig(labels=[MODEL_TEXT])}
    # 绑的是模型下拉的文案
    assert "模型下拉" in _misbind_hint("thinking", [MODEL_TEXT], ui, [MODEL_TEXT])
    # 两个开关撞同一个控件（哪怕没撞上模型文案）
    other = {"thinking": UiControlConfig(labels=["代码"]), "search": UiControlConfig(labels=["代码"])}
    assert "同一个控件" in _misbind_hint("search", ["代码"], other, [])
    # 正常绑定不该报警；model 这一项本来就该和开关同文案，不参与撞车判定
    assert _misbind_hint("thinking", ["深度思考"], {"thinking": UiControlConfig(labels=["深度思考"])}, []) == ""
    assert _misbind_hint("thinking", ["快速"], {"model": UiControlConfig(labels=["快速"])}, []) == ""
    assert _misbind_hint("thinking", [], {}, [MODEL_TEXT]) == ""


@pytest.mark.asyncio
async def test_misbind_model_text_never_touches_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """绑成模型文案：开和关都不该去点，否则每轮都在点开模型菜单。"""

    def _boom(*_a, **_k):  # pragma: no cover - 被调用即失败
        raise AssertionError("误绑的开关不该去点页面")

    monkeypatch.setattr(ui_modes, "_set_toggle", _boom)
    plan = ModePlan(preset_id="quick", features={"thinking": False, "search": False})
    res = await apply_mode(_FakePage(), "qwen", plan, _qwen_cfg())
    assert res["ok"] is True
    assert res["skipped"] == ["thinking", "search"]
    assert res["misbind"] == ["thinking", "search"]
    assert all("疑似绑错" in (d.get("reason") or "") for d in res["details"])

    # 目标为开也一样拒绝：一个鼠标事件都不能发出去
    up = ModePlan(preset_id="think", features={"thinking": True})
    res2 = await apply_mode(_FakePage(), "qwen", up, _qwen_cfg())
    assert res2["ok"] is False
    assert res2["missing"] == ["thinking"] and res2["misbind"] == ["thinking"]
    assert "已拒绝点击" in res2["details"][0]["reason"]


@pytest.mark.asyncio
async def test_misbind_other_overlap_still_clicks_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """不是模型文案的撞车（思考/联网绑了同一个场景按钮）：该点还得点，但原因要先说清。"""

    async def _fake_toggle(_page, feature, want, _ctl):
        return {"feature": feature, "want": want, "ok": False, "reason": "状态不可判定"}

    monkeypatch.setattr(ui_modes, "_set_toggle", _fake_toggle)
    cfg = _cfg(ui={
        "thinking": UiControlConfig(labels=["代码"]),
        "search": UiControlConfig(labels=["代码"]),
    })
    plan = ModePlan(preset_id="think", features={"thinking": True})
    res = await apply_mode(_FakePage(), "qwen", plan, cfg)
    assert res["ok"] is False
    assert res["missing"] == ["thinking"]
    assert res["misbind"] == ["thinking"]
    assert res["details"][0]["reason"].startswith("疑似绑错")
    assert "同一个控件" in res["details"][0]["reason"]


@pytest.mark.asyncio
async def test_misbind_that_happens_to_work_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """万一真点成功了（文案复用）：不算失败，但得留下 hint 提示下次改版会失效。"""

    async def _ok_toggle(_page, feature, want, _ctl):
        return {"feature": feature, "want": want, "ok": True, "action": "已点击"}

    monkeypatch.setattr(ui_modes, "_set_toggle", _ok_toggle)
    cfg = _cfg(ui={
        "thinking": UiControlConfig(labels=["代码"]),
        "search": UiControlConfig(labels=["代码"]),
    })
    plan = ModePlan(preset_id="think", features={"thinking": True})
    res = await apply_mode(_FakePage(), "qwen", plan, cfg)
    assert res["ok"] is True
    assert res["misbind"] == []
    assert "疑似绑错" in res["details"][0]["hint"]
