"""页面「绑定」覆盖层：把 /live 面板里选中的控件文案存到 storage/ui-bindings.yaml。

为什么不直接改 config.yaml：那份文件是人工维护的，有注释和手写字段，yaml.safe_dump
重写会全部丢光。这里单独存一层覆盖，reload 时合并进内存配置：

- ui.<feature>：整段覆盖（thinking / search / model，以及页面里新加的自定义场景键，
  如 code / translate：页面上看到的就是最终绑定的）；
- models.<id>.label：先全部置空再按提交值写入（留空 = 不驱动模型切换）；
- modes：标准四模式 + 每个自定义场景各一个模式，features 按绑定自动推导；
  body_patch / label 仍沿用 config.yaml。

删掉这个文件（或 DELETE /live/ui-bindings/<key>）即完全回到 config.yaml。
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# 内置控件；model 只是下拉触发器，不参与开关推导
STANDARD_FEATURES = ("thinking", "search", "model")
# 自定义场景键不能覆盖内置控件/模式名
_RESERVED = {"thinking", "search", "model", "quick", "think", "expert"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_MAX_LABELS = 6
_MAX_LABEL_LEN = 40
_MAX_CUSTOM = 8


def bindings_path(settings) -> Path:
    """覆盖层文件位置：storage/ui-bindings.yaml（已被 gitignore）。"""
    return settings.resolved_storage_dir() / "ui-bindings.yaml"


def _clean_labels(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        text = str(item or "").strip()[:_MAX_LABEL_LEN]
        if text and text not in out:
            out.append(text)
    return out[:_MAX_LABELS]


def normalize(payload: dict) -> dict:
    """只保留白名单字段；键存在即表示「这一部分由页面接管」。"""
    out: dict[str, Any] = {}
    if "ui" in payload:
        ui_in = payload.get("ui") or {}
        ui: dict[str, dict] = {}
        custom = 0
        if isinstance(ui_in, dict):
            for name, ctl in ui_in.items():
                key = str(name or "").strip()
                if not isinstance(ctl, dict):
                    continue
                if key not in STANDARD_FEATURES:
                    # 自定义场景：只接受 ASCII 小写键名，且不能与内置控件/模式重名
                    if not _KEY_RE.match(key) or key in _RESERVED:
                        continue
                labels = _clean_labels(ctl.get("labels"))
                if not labels:
                    continue
                if key not in STANDARD_FEATURES:
                    # 只给真正会生效的条目计数，避免空标签占满名额
                    custom += 1
                    if custom > _MAX_CUSTOM:
                        continue
                item: dict[str, Any] = {"labels": labels}
                active = str(ctl.get("active_class") or "").strip()[:200]
                if active:
                    item["active_class"] = active
                if ctl.get("blind"):
                    item["blind"] = True
                ui[key] = item
        out["ui"] = ui
    if "models" in payload:
        models_in = payload.get("models") or {}
        labels: dict[str, str] = {}
        if isinstance(models_in, dict):
            for mid, label in models_in.items():
                model_id = str(mid or "").strip()
                text = str(label or "").strip()[:_MAX_LABEL_LEN]
                if model_id and text:
                    labels[model_id] = text
        out["models"] = labels
    return out


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 文件被手改坏了也不能拖垮启动
        logger.warning(f"[ui-bindings] 读取失败，按空覆盖处理: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def save_provider(path: Path, key: str, payload: dict) -> dict:
    """写入某个 provider 的绑定；payload 为空表示移除该 provider 的覆盖。"""
    entry = normalize(payload or {})
    data = load(path)
    if entry:
        data[key] = entry
    else:
        data.pop(key, None)
    if data:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    elif path.exists():
        path.unlink()
    return entry


def _rebuild_modes(old: Any, ui: dict) -> dict:
    """按已绑定的开关重建模式；body_patch / label 从原配置继承。

    标准四模式由 thinking/search 推导；每个自定义场景（如 code）额外生成一个同名模式，
    并写进 quick 的 features=false，这样下一轮快速请求会把上一轮的场景标签关掉。
    """
    old_modes = old if isinstance(old, dict) else {}
    names = set(ui)
    custom = [n for n in names if n not in STANDARD_FEATURES]

    def spec(name: str, feats: dict[str, bool]) -> dict:
        base = dict(old_modes.get(name) or {})
        base["features"] = feats
        if not base.get("label") and (ui.get(name) or {}).get("labels"):
            base["label"] = ui[name]["labels"][0]
        return base

    quick_feats: dict[str, bool] = {}
    if "thinking" in names:
        quick_feats["thinking"] = False
    if "search" in names:
        quick_feats["search"] = False
    for name in custom:
        quick_feats[name] = False

    out: dict[str, dict] = {"quick": spec("quick", quick_feats)}
    if "thinking" in names:
        out["think"] = spec("think", {"thinking": True})
    if "search" in names:
        out["search"] = spec("search", {"search": True})
    if {"thinking", "search"} <= names:
        out["expert"] = spec("expert", {"thinking": True, "search": True})
    for name in custom:
        out[name] = spec(name, {name: True})
    # 保留用户自定义的非标准模式；标准模式与本次绑定的场景模式一律由本次重建，
    # 否则「页面上解绑了 search」还会留一个永远缺失的 search 模式。
    standard = {"quick", "think", "search", "expert"}
    for name, spec_in in old_modes.items():
        if name not in out and name not in standard:
            out[name] = spec_in
    return out


def merge(raw: dict, overrides: dict) -> dict:
    """把覆盖层合并进 config.yaml 的原始 dict（深拷贝，不改入参）。"""
    if not overrides:
        return raw
    data = copy.deepcopy(raw)
    providers = data.get("providers") or {}
    if not isinstance(providers, dict):
        return data
    for key, entry in overrides.items():
        pcfg = providers.get(key)
        if not isinstance(pcfg, dict) or not isinstance(entry, dict):
            continue
        if "ui" in entry:
            ui = {k: dict(v) for k, v in (entry.get("ui") or {}).items() if isinstance(v, dict)}
            pcfg["ui"] = ui
            pcfg["modes"] = _rebuild_modes(pcfg.get("modes"), ui)
        if "models" in entry:
            labels = entry.get("models") or {}
            for item in pcfg.get("models") or []:
                if isinstance(item, dict):
                    item["label"] = labels.get(item.get("id"), "")
    return data
