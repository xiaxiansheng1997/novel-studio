"""模式解析：别名/参数优先级、deepseek-reasoner 与 -think 等价、未知 mode 降级。"""
from __future__ import annotations

from types import SimpleNamespace

from app.config import ModeSpecConfig, ModelDef, ProviderConfig
from app.core.modes import resolve_mode, split_model


def _pcfg() -> ProviderConfig:
    return ProviderConfig(
        key="deepseek",
        models=[
            ModelDef(id="deepseek-chat", label="V3"),
            ModelDef(id="deepseek-reasoner", label="R1", features={"thinking": True}),
        ],
        modes={
            "quick": ModeSpecConfig(label="快速", features={"thinking": False, "search": False}),
            "think": ModeSpecConfig(label="深度思考", features={"thinking": True}),
            "search": ModeSpecConfig(label="联网", features={"search": True}),
            "expert": ModeSpecConfig(label="专家", features={"thinking": True, "search": True}),
        },
    )


def _req(**kw) -> SimpleNamespace:
    base = {"model": "deepseek-chat"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_alias_suffix_sets_feature() -> None:
    plan = resolve_mode(_pcfg(), _req(model="deepseek-chat-think"))
    assert plan.base_model == "deepseek-chat"
    assert plan.preset_id == "think"
    assert plan.features["thinking"] is True


def test_reasoner_equals_think_suffix() -> None:
    pcfg = _pcfg()
    by_model = resolve_mode(pcfg, _req(model="deepseek-reasoner"))
    by_suffix = resolve_mode(pcfg, _req(model="deepseek-chat-think"))
    assert by_model.preset_id == by_suffix.preset_id == "think"
    assert by_model.features == by_suffix.features


def test_explicit_param_beats_model_name() -> None:
    """参数优先级高于模型名后缀：-think 但 thinking=false，最终必须是关。"""
    plan = resolve_mode(_pcfg(), _req(model="deepseek-chat-think", thinking=False))
    assert plan.features["thinking"] is False


def test_reasoning_effort_medium_enables_thinking() -> None:
    plan = resolve_mode(_pcfg(), _req(reasoning_effort="medium"))
    assert plan.features["thinking"] is True


def test_unknown_mode_degrades_to_quick() -> None:
    plan = resolve_mode(_pcfg(), _req(mode="nonsense"))
    assert plan.preset_id == "quick"
    assert plan.features.get("thinking") is False
    assert any("未知 mode" in r for r in plan.reasons)


def test_unknown_suffix_not_treated_as_mode() -> None:
    """未声明的模型 + 非模式后缀，不能被猜成模式。"""
    base, toks = split_model(_pcfg().models, "deepseek-unknown")
    assert base is None
    assert toks == []


def test_both_features_on_maps_to_expert() -> None:
    plan = resolve_mode(_pcfg(), _req(thinking=True, search=True))
    assert plan.preset_id == "expert"
