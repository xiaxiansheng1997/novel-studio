"""页面绑定覆盖层的纯函数测试：白名单、落盘、合并语义。"""
from __future__ import annotations

from app.core import ui_bindings as ub


def test_normalize_keeps_whitelisted_features():
    entry = ub.normalize(
        {
            "ui": {
                "thinking": {"labels": [" 深度思考 ", "深度思考", ""]},
                "search": {"labels": []},
                "model": {"labels": ["切换模型"], "active_class": "active", "blind": True},
                "code": {"labels": ["代码"]},
                "Bad Key": {"labels": ["x"]},
            },
            "models": {"qwen-max": "Qwen3.7", "": "x"},
        }
    )
    assert entry["ui"]["thinking"]["labels"] == ["深度思考"]
    assert "search" not in entry["ui"]  # 空文案不绑定
    assert entry["ui"]["model"] == {"labels": ["切换模型"], "active_class": "active", "blind": True}
    assert entry["ui"]["code"] == {"labels": ["代码"]}  # 自定义场景
    assert "Bad Key" not in entry["ui"]
    assert entry["models"] == {"qwen-max": "Qwen3.7"}


def test_normalize_rejects_reserved_and_invalid_custom_keys():
    entry = ub.normalize(
        {
            "ui": {
                "quick": {"labels": ["快速"]},
                "think": {"labels": ["思考"]},
                "expert": {"labels": ["专家"]},
                "9code": {"labels": ["代码"]},
                "a" * 25: {"labels": ["x"]},
                "translate": {"labels": ["翻译"]},
            }
        }
    )
    assert set(entry["ui"]) == {"translate"}


def test_normalize_caps_custom_features():
    ui = {f"f{i}": {"labels": [f"场景{i}"]} for i in range(12)}
    entry = ub.normalize({"ui": ui})
    assert len(entry["ui"]) == ub._MAX_CUSTOM


def test_normalize_marks_empty_sections():
    assert ub.normalize({"ui": {}, "models": {}}) == {"ui": {}, "models": {}}
    assert ub.normalize({}) == {}


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "ui-bindings.yaml"
    ub.save_provider(path, "qwen", {"ui": {"thinking": {"labels": ["深度思考"]}}})
    ub.save_provider(path, "doubao", {"models": {"doubao-pro": "豆包 Pro"}})
    data = ub.load(path)
    assert data["qwen"]["ui"]["thinking"]["labels"] == ["深度思考"]
    assert data["doubao"]["models"] == {"doubao-pro": "豆包 Pro"}

    ub.save_provider(path, "qwen", {})
    assert "qwen" not in ub.load(path)
    ub.save_provider(path, "doubao", {})
    assert not path.exists()


def _raw():
    return {
        "providers": {
            "qwen": {
                "models": [{"id": "qwen-max", "label": "Qwen3-Max"}],
                "modes": {
                    "quick": {
                        "label": "快速模式",
                        "features": {"thinking": False},
                        "body_patch": {"chat_mode": "quick"},
                    },
                    "think": {"label": "深度思考", "features": {"thinking": True}},
                    "search": {"label": "联网搜索", "features": {"search": True}},
                },
                "ui": {"thinking": {"labels": ["深度思考"]}},
            }
        }
    }


def test_merge_rebuilds_modes_and_keeps_body_patch():
    raw = _raw()
    out = ub.merge(raw, {"qwen": {"ui": {"thinking": {"labels": ["快速"]}}}})
    qwen = out["providers"]["qwen"]

    assert qwen["ui"] == {"thinking": {"labels": ["快速"]}}
    assert qwen["modes"]["quick"]["features"] == {"thinking": False}
    assert qwen["modes"]["quick"]["body_patch"] == {"chat_mode": "quick"}
    assert qwen["modes"]["quick"]["label"] == "快速模式"
    assert qwen["modes"]["think"]["features"] == {"thinking": True}
    assert "search" not in qwen["modes"]  # 未绑定的开关不再保留假模式
    assert raw["providers"]["qwen"]["ui"] == {"thinking": {"labels": ["深度思考"]}}  # 不改入参


def test_merge_generates_expert_when_both_bound():
    raw = _raw()
    out = ub.merge(
        raw,
        {"qwen": {"ui": {"thinking": {"labels": ["深度思考"]}, "search": {"labels": ["搜索"]}}}},
    )
    modes = out["providers"]["qwen"]["modes"]
    assert modes["quick"]["features"] == {"thinking": False, "search": False}
    assert modes["expert"]["features"] == {"thinking": True, "search": True}


def test_merge_models_clears_then_sets_labels():
    raw = _raw()
    out = ub.merge(raw, {"qwen": {"models": {"qwen-max": "Qwen3.7"}}})
    assert out["providers"]["qwen"]["models"][0]["label"] == "Qwen3.7"

    cleared = ub.merge(raw, {"qwen": {"models": {}}})
    assert cleared["providers"]["qwen"]["models"][0]["label"] == ""


def test_merge_generates_custom_mode_and_quick_off():
    raw = _raw()
    out = ub.merge(raw, {"qwen": {"ui": {"thinking": {"labels": ["深度思考"]}, "code": {"labels": ["代码"]}}}})
    qwen = out["providers"]["qwen"]

    assert qwen["ui"]["code"] == {"labels": ["代码"]}
    assert qwen["modes"]["quick"]["features"] == {"thinking": False, "code": False}
    assert qwen["modes"]["code"]["features"] == {"code": True}
    assert qwen["modes"]["code"]["label"] == "代码"  # 模式显示名取场景文案


def test_merge_custom_mode_keeps_body_patch_from_config():
    raw = _raw()
    raw["providers"]["qwen"]["modes"]["code"] = {
        "label": "代码模式",
        "features": {"code": True},
        "body_patch": {"chat_mode": "code"},
    }
    out = ub.merge(raw, {"qwen": {"ui": {"code": {"labels": ["代码"]}}}})
    code = out["providers"]["qwen"]["modes"]["code"]
    assert code["label"] == "代码模式"
    assert code["body_patch"] == {"chat_mode": "code"}
