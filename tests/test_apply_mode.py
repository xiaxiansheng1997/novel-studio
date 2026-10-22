"""页面模式对齐：provider 没配控件时，只有「要开」才算未对齐。

前端每轮请求都会带上 thinking/search 的布尔值（关的时候是 false），
千问这类页面本来就没有这两个开关，若把 want=False 也记 missing，
管理页与日志会每轮报「未对齐」，真正的配置缺失反而被淹没。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.browser.ui_modes import apply_mode
from app.core.modes import ModePlan


class _FakePage:
    """只够读 url；任何 DOM 操作都会抛异常（对齐逻辑不该碰页面）。"""

    url = "https://chat.qwen.ai/c/x"

    def __getattr__(self, name):  # noqa: ANN001 - 兜住所有未实现的方法
        raise AttributeError(name)


def _cfg() -> SimpleNamespace:
    # ui 为空：等价于千问这种「页面没有思考/联网开关」的 provider
    return SimpleNamespace(ui={}, models=[])


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
