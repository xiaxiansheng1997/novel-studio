"""内容类型配置：把「文种差异」从代码里搬到 YAML。

为什么要有这一层：五类文档只是举例，真实需求会长出新文种（讲义、播客稿、
复盘、招标解读……）。如果每个文种都要改 Python 分支，加一次类型就要改三处
（模板、装配预算、检查规则），迟早漏。这里统一成一份配置 + 一次合并。

合并顺序（后者覆盖前者，列表默认追加）：
    kinds.<kind>  →  kinds.<kind>.scales.<scale>  →  kinds.<kind>.subtypes.<subtype>

约定的字段：
    label            显示名
    budget_chars     单次装配总预算（字符）
    buckets          启用哪些上下文桶
    layers           节点层级 [{kind,label,readable}]
    style_prompt     进 S1 的文体要求
    instructions     进 S1 的额外约束（通常来自 subtype）
    checks           检查规则名列表
    template         默认模板 id（subtype 层给）
    requires_sources 没有素材时是否强提示
    fresh_days       素材时效半衰期（天）

YAML 改完不用重启：`get_types()` 按 mtime 判断缓存是否失效。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = BASE_DIR / "content-types.yaml"

# 兜底：YAML 丢了也要能起来，否则一个配置文件错误会让整个写作台打不开
_FALLBACK: dict[str, Any] = {
    "version": 1,
    "kinds": {
        "article": {
            "label": "文档",
            "budget_chars": 18000,
            "buckets": ["S0", "S1", "S2", "S_src", "S7", "S8"],
            "style_prompt": "用书面中文写作，句子求短，具体名词优先。",
            "checks": ["structure_incomplete"],
            "layers": [{"kind": "section", "label": "节", "readable": True}],
            "scales": {"single": {"label": "单篇"}},
            "subtypes": {"general": {"label": "通用", "template": "tpl_general"}},
        },
        "novel": {
            "label": "小说",
            "budget_chars": 28000,
            "buckets": ["S1", "S2", "S7", "S8"],
            "style_prompt": "第三人称叙事，多写动作与细节。",
            "checks": ["time_regression", "address_drift", "setting_conflict"],
            "layers": [
                {"kind": "volume", "label": "卷", "readable": False},
                {"kind": "chapter", "label": "章", "readable": True},
            ],
            "scales": {"single": {"label": "单本"}},
            "subtypes": {},
        },
    },
    "templates": [],
}

_cache: dict[str, Any] = {"mtime": -1.0, "data": _FALLBACK, "path": ""}


def _merge(base: dict, over: Optional[dict]) -> dict:
    """合并一层配置。`xxx_append` 追加到 `xxx`，其余按标量覆盖。"""
    out = dict(base)
    if not over:
        return out
    for key, val in over.items():
        if key.endswith("_append"):
            real = key[: -len("_append")]
            cur = list(out.get(real) or [])
            for item in val or []:
                if item not in cur:  # 去重：三层可能写同一条
                    cur.append(item)
            out[real] = cur
            continue
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
            continue
        out[key] = val
    return out


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _FALLBACK
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (yaml.YAMLError, OSError):
        # 配置写坏了不能让服务崩：退回兜底 + 保留错误信息供接口提示
        return {**_FALLBACK, "_load_error": "content-types.yaml 解析失败，已使用内置兜底"}
    if not isinstance(data, dict) or "kinds" not in data:
        return {**_FALLBACK, "_load_error": "content-types.yaml 缺少 kinds 段，已使用内置兜底"}
    # 只保留 article/novel 两种 kind 的键，避免用户误写别的顶层键
    data.setdefault("templates", [])
    return data


def get_types(path: Optional[Path] = None) -> dict[str, Any]:
    """按 mtime 缓存；改完 YAML 直接生效，不用重启。"""
    p = path or Path(os.environ.get("W2A_CONTENT_TYPES") or DEFAULT_PATH)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = -1.0
    if mtime != _cache["mtime"] or str(p) != _cache["path"]:
        _cache.update({"mtime": mtime, "data": _load(p), "path": str(p)})
    return _cache["data"]


def reload_types() -> dict[str, Any]:
    """强制重新读盘（改完 YAML 手动触发）。"""
    _cache["mtime"] = -1.0
    return get_types()


def _raw_kind(kind: str) -> dict[str, Any]:
    kinds = get_types().get("kinds") or {}
    return dict(kinds.get(kind) or kinds.get("article") or {})


def resolve(kind: str, scale: str = "", subtype: str = "") -> dict[str, Any]:
    """三层合并出该作品的实际配置。"""
    base = _raw_kind(kind)
    scales = base.get("scales") or {}
    if not scale:
        scale = next(iter(scales), "single")
    sc = _merge(base, scales.get(scale) or {})

    subtypes = base.get("subtypes") or {}
    if not subtype:
        subtype = next(iter(subtypes), "")
    merged = _merge(sc, subtypes.get(subtype) or {})

    layers = merged.get("layers") or [{"kind": "section", "label": "节", "readable": True}]
    readable = [layer for layer in layers if layer.get("readable", True)]
    checks = list(merged.get("checks") or [])
    # 检查规则去重但不排序：顺序按「重要的先报」
    seen: list[str] = []
    for c in checks:
        if c not in seen:
            seen.append(c)

    return {
        "kind": kind,
        "scale": scale,
        "subtype": subtype,
        "label": merged.get("label") or kind,
        "subtype_label": (subtypes.get(subtype) or {}).get("label") or subtype,
        "budget_chars": int(merged.get("budget_chars") or 18000),
        "buckets": list(merged.get("buckets") or ["S0", "S1", "S2", "S_src", "S7", "S8"]),
        "style_prompt": merged.get("style_prompt") or "",
        "instructions": merged.get("instructions") or "",
        "checks": seen,
        "layers": layers,
        "leaf_kind": (readable[0] if readable else {}).get("kind", "section"),
        "container_kind": next(
            (layer.get("kind") for layer in layers if not layer.get("readable", True)), None
        ),
        "template": merged.get("template") or "",
        "requires_sources": bool(merged.get("requires_sources")),
        "fresh_days": int(merged.get("fresh_days") or 0),
        "target_words": int(merged.get("target_words") or 0),
        "note": merged.get("note") or "",
    }


def list_kinds() -> list[dict[str, Any]]:
    """给前端下拉框用：kind → scales → subtypes 的树。"""
    out: list[dict[str, Any]] = []
    for key, raw in (get_types().get("kinds") or {}).items():
        if not isinstance(raw, dict):
            continue
        scales = []
        for skey, sval in (raw.get("scales") or {}).items():
            resolved = resolve(key, skey, "")
            scales.append({
                "id": skey,
                "label": (sval or {}).get("label") or skey,
                "target_words": resolved.get("target_words", 0),
                "note": resolved.get("note", ""),
            })
        subtypes = [
            {"id": stkey, "label": (stval or {}).get("label") or stkey}
            for stkey, stval in (raw.get("subtypes") or {}).items()
            if isinstance(stval, dict)
        ]
        out.append({
            "id": key,
            "label": raw.get("label") or key,
            "budget_chars": int(raw.get("budget_chars") or 0),
            "scales": scales,
            "subtypes": subtypes,
        })
    return out


def template_id_for(kind: str, scale: str = "", subtype: str = "") -> str:
    return str(resolve(kind, scale, subtype).get("template") or "")


def templates() -> list[dict[str, Any]]:
    """内置模板；从 YAML 读，缺字段时给安全默认值。"""
    out = []
    for tpl in get_types().get("templates") or []:
        if not isinstance(tpl, dict) or not tpl.get("id"):
            continue
        outline = tpl.get("outline") or []
        if not isinstance(outline, list):
            outline = []
        out.append({
            "id": str(tpl["id"]),
            "kind": str(tpl.get("kind") or "article"),
            "subtype": str(tpl.get("subtype") or ""),
            "name": str(tpl.get("name") or tpl["id"]),
            "outline": [o for o in outline if isinstance(o, dict)],
            "style": str(tpl.get("style") or ""),
            "checks": list(tpl.get("checks") or []),
        })
    return out


def find_template(template_id: str) -> Optional[dict[str, Any]]:
    for tpl in templates():
        if tpl["id"] == template_id:
            return tpl
    return None
