"""检索：把 query 变成证据片段。

两路召回：正文块（`chunks_fts`）+ 节摘要（`summaries_fts`），用 RRF 融合。
摘要路专门补「第 30 章埋的伏笔」这类距今很远、原文块已经排不进前几名的证据；
最近几节不重复召回——那几条摘要 S4 已经整块注入过了。

一个关键的重排：**距离当前节点越近的块权重越高**，但用的是乘性系数而不是
绝对加项。绝对加项在 base 很小时（长库里排第 20 名的精确命中 base≈0.05）会让
近因直接主导排序，实测 2000 章下 top8 全落在当前节附近。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from app.studio import tokenize

# 距离衰减的半衰期（节点数）：相隔 20 节约剩 37%
HALF_LIFE = 20.0
# 近因加成上限（乘在归一化分数上）：近处最多 +15%
RECENCY_WEIGHT = 0.15
# S4 已经整块注入的最近节数：摘要路不再重复召回这几节
RECENT_WINDOW = 6
# 计划里的地点/标题在候选块里原文命中时给的加成（短语优先）
PHRASE_BONUS = 0.15
# 摘要路最多占几条：摘要是概述，不能把原文块挤掉
SUMMARY_SLOTS = 3


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


def _recency_factor(ordinal: int, current: Optional[int]) -> float:
    """乘性近因：`base * (1 + w * exp(-gap / half_life))`。

    乘而不是加：加项会「制造」相关（base≈0 的块加 0.35 也能挤进 top8），
    乘项只放大本来就相关的块，远处的精确命中不会被近处的不相关块越过去。
    """
    if not current or not ordinal:
        return 1.0
    gap = abs(ordinal - current)
    return 1.0 + RECENCY_WEIGHT * math.exp(-gap / HALF_LIFE)


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
    phrases: Optional[list[str]] = None,
    use_summaries: bool = True,
) -> list[dict[str, Any]]:
    """召回证据片段，按「相关度 + 近因」排序，按预算截断。

    两路：正文块 + 节摘要。摘要路只在检索正文时启用——素材桶要的是原文，
    不是自己写的梗概。两路都用 RRF 名次融合；只有一路时退回 BM25 归一，
    与改造前行为一致（单路也套 RRF 会把「差多少分」这层信息抹掉）。
    """
    # 这里的 query 是「本节计划 + 指令模板」拼的，模板套话要剔掉；
    # 跨作品搜索走的是同一个 match_query，那边必须保留（见 tokenize 里的注释）
    match = tokenize.match_query(query, drop_template=True)
    if not match:
        return []

    # 多取一些用于去重与重排（同一节可能贡献多个块）
    raw = await store.search_chunks(work_id, match, kinds=kinds, limit=max(top_k * 4, 24))
    summaries: list[dict[str, Any]] = []
    if use_summaries and kinds == ("body",) and current_ordinal:
        # 最近 RECENT_WINDOW 节交给 S4，这里只要「更早的相关前情」
        summaries = await store.search_summaries(
            work_id, match, limit=max(top_k, 12),
            max_ordinal=current_ordinal - RECENT_WINDOW,
        )
    if not raw and not summaries:
        return []

    titles = await store.get_chunk_source_titles(work_id)
    exclude = exclude_node_ids or set()
    phrase_list = [p for p in (phrases or []) if len(p) >= 3]

    chunk_items: list[dict[str, Any]] = []
    for c in raw:
        src_key = c.get("node_id") or c.get("source_id") or ""
        chunk_items.append({
            "id": c["id"],
            "chunk_id": c["id"],
            "text": c.get("text") or "",
            "source_kind": c.get("source_kind"),
            "node_id": c.get("node_id"),
            "source_id": c.get("source_id"),
            "ordinal": int(c.get("ordinal") or 0),
            # 素材块没有章号，出处只能靠「第几段 + 原文第几字起」；
            # 节点块用不上，但多两个字段比给素材单开一条通道便宜
            "seq": int(c.get("seq_in_unit") or 0),
            "start": int(c.get("char_start") or 0),
            "title": titles.get(src_key) or "未命名",
            "score": float(c.get("score") or 0.0),
        })

    summary_items: list[dict[str, Any]] = []
    for s in summaries:
        rid = s.get("ref_id") or ""
        summary_items.append({
            "id": f"sum_{s['id']}",
            "chunk_id": f"sum_{s['id']}",
            "text": s.get("content") or "",
            "source_kind": "summary",
            "node_id": rid,
            "source_id": None,
            "ordinal": int(s.get("ref_ordinal") or 0),
            "seq": 0,
            "start": 0,
            "title": titles.get(rid) or "未命名",
            "score": float(s.get("score") or 0.0),
        })

    if chunk_items and summary_items:
        merged = fuse([chunk_items, summary_items])
        peak = max((it.get("rrf") or 0.0) for it in merged) or 1.0
        for it in merged:
            it["_base"] = float(it.get("rrf") or 0.0) / peak
    else:
        merged = chunk_items or summary_items
        peak = max((it.get("score") or 0.0) for it in merged) or 1.0
        for it in merged:
            it["_base"] = float(it.get("score") or 0.0) / peak

    seen: set[str] = set()
    per_source: dict[str, int] = {}
    summary_used = 0
    scored: list[dict[str, Any]] = []
    for c in merged:
        if c.get("node_id") and c["node_id"] in exclude:
            continue
        key = (c.get("text") or "")[:60]
        if key in seen:
            continue
        seen.add(key)

        is_summary = c.get("source_kind") == "summary"
        if is_summary and summary_used >= SUMMARY_SLOTS:
            continue

        src_key = c.get("node_id") or c.get("source_id") or ""
        if per_source.get(src_key, 0) >= max_per_source:
            continue
        per_source[src_key] = per_source.get(src_key, 0) + 1
        if is_summary:
            summary_used += 1

        base = float(c.get("_base") or 0.0)
        # 短语优先：地点/标题这种短串在候选块里原文出现，就是比散词命中硬的证据
        if phrase_list and any(p in (c.get("text") or "") for p in phrase_list):
            base = min(1.0, base + PHRASE_BONUS)
        factor = _recency_factor(int(c.get("ordinal") or 0), current_ordinal) \
            if kinds == ("body",) else 1.0
        scored.append({
            "id": c["id"],
            "chunk_id": c.get("chunk_id") or c["id"],
            "text": c.get("text") or "",
            "source_kind": c.get("source_kind"),
            "node_id": c.get("node_id"),
            "source_id": c.get("source_id"),
            "ordinal": int(c.get("ordinal") or 0),
            "seq": int(c.get("seq") or 0),
            "start": int(c.get("start") or 0),
            "title": c.get("title") or "未命名",
            "score": round(base * factor, 4),
            "bm25": round(float(c.get("score") or 0.0), 4),
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
        # 摘要不是原文：不标出来，模型会当成「书里就是这么写的」直接引用
        if it.get("source_kind") == "summary":
            where += " · 摘要"
        lines.append(f"【{it.get('title') or '未命名'} · {where}】\n{it.get('text', '').strip()}")
    return "\n\n---\n\n".join(lines)
