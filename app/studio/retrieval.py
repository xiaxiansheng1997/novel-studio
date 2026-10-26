"""检索：把 query 变成证据片段。

M2 只有一路召回（FTS5 + jieba），所以先不做真正的 RRF 融合，
但把 `fuse()` 的位置留出来——M5 加向量时只需多一路 rank，不用改调用方。

一个关键的重排：**距离当前节点越近的块权重越高**。
写作场景里「上一章刚提到的设定」比「第 3 章提过一次」更该被引用，
纯 BM25 对两者一视同仁，加一个按 ordinal 距离的指数衰减项就能显著改善。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from app.studio import tokenize

# 距离衰减的半衰期（节点数）：相隔 20 节约剩 37%
HALF_LIFE = 20.0
# 近因加成上限（相对归一化后的 BM25 分数）
RECENCY_WEIGHT = 0.35


def build_query(*parts: Optional[str], max_chars: int = 400) -> str:
    """把标题、大纲要点、用户指令拼成检索 query。"""
    buf: list[str] = []
    size = 0
    for p in parts:
        if not p:
            continue
        text = str(p).strip()
        if not text:
            continue
        buf.append(text)
        size += len(text)
        if size >= max_chars:
            break
    return " ".join(buf)[:max_chars]


def _recency_boost(ordinal: int, current: Optional[int]) -> float:
    if not current or not ordinal:
        return 0.0
    gap = abs(ordinal - current)
    return RECENCY_WEIGHT * math.exp(-gap / HALF_LIFE)


def fuse(rankings: Iterable[list[dict[str, Any]]], *, k: int = 60) -> list[dict[str, Any]]:
    """RRF 融合。M2 只有一路，保留接口给 M5 的向量召回用。"""
    scores: dict[str, float] = {}
    keep: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            key = item.get("id") or item.get("chunk_id") or str(item.get("text", ""))[:32]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            keep.setdefault(key, item)
    merged = []
    for key, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        item = dict(keep[key])
        item["rrf"] = round(s, 6)
        merged.append(item)
    return merged


async def retrieve(
    store,
    work_id: str,
    *,
    query: str,
    current_ordinal: Optional[int] = None,
    kinds: tuple[str, ...] = ("body",),
    top_k: int = 8,
    budget_chars: int = 6000,
    exclude_node_ids: Optional[set[str]] = None,
    max_per_source: int = 2,
) -> list[dict[str, Any]]:
    """召回证据片段，按「相关度 + 近因」排序，按预算截断。"""
    match = tokenize.match_query(query)
    if not match:
        return []

    # 多取一些用于去重与重排（同一节可能贡献多个块）
    raw = await store.search_chunks(work_id, match, kinds=kinds, limit=max(top_k * 4, 24))
    if not raw:
        return []

    titles = await store.get_chunk_source_titles(work_id)
    peak = max((c["score"] for c in raw), default=0.0) or 1.0
    exclude = exclude_node_ids or set()

    seen: set[str] = set()
    per_source: dict[str, int] = {}
    scored: list[dict[str, Any]] = []
    for c in raw:
        if c.get("node_id") and c["node_id"] in exclude:
            continue
        key = (c.get("text") or "")[:60]
        if key in seen:
            continue
        seen.add(key)

        src_key = c.get("node_id") or c.get("source_id") or ""
        if per_source.get(src_key, 0) >= max_per_source:
            continue
        per_source[src_key] = per_source.get(src_key, 0) + 1

        base = float(c["score"]) / peak
        bonus = _recency_boost(int(c.get("ordinal") or 0), current_ordinal) if kinds == ("body",) else 0.0
        scored.append({
            "id": c["id"],
            "chunk_id": c["id"],
            "text": c.get("text") or "",
            "source_kind": c.get("source_kind"),
            "node_id": c.get("node_id"),
            "source_id": c.get("source_id"),
            "ordinal": int(c.get("ordinal") or 0),
            "title": titles.get(src_key) or "未命名",
            "score": round(base + bonus, 4),
            "bm25": round(float(c["score"]), 4),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    picked: list[dict[str, Any]] = []
    used = 0
    for item in scored:
        if len(picked) >= top_k:
            break
        if used + len(item["text"]) > budget_chars and picked:
            continue
        picked.append(item)
        used += len(item["text"])
    return picked


def format_evidence(items: list[dict[str, Any]], *, note: str = "") -> str:
    """把证据拼成进 prompt 的文本块。带出处，方便模型引用也方便人排查。"""
    if not items:
        return ""
    lines = [f"<!-- {note} -->"] if note else []
    for it in items:
        where = f"第 {it['ordinal']} 节" if it.get("ordinal") else "素材"
        lines.append(f"【{it.get('title') or '未命名'} · {where}】\n{it.get('text', '').strip()}")
    return "\n\n---\n\n".join(lines)
