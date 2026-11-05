"""索引构建：正文与素材 → chunks + chunks_fts。

增量策略：`index_units` 里记每个节点/素材的 content_hash，只重建变过的，
所以「边写边索引」的成本只有当前这一节（几毫秒），不需要等整本写完。

为什么素材单独标记 `source_kind='source'`：S_src 桶只允许引用素材，
S5 只允许引用正文，混在一起会让模型把「自己上一章写的话」当成外部事实。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.studio import tokenize
from app.studio.chunker import split_text


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# 批量重建每多少个单元提交一次：一次提交在 Windows 上实测约 7ms（刷盘），
# 逐单元提交会让「给整本建索引」变成纯等 IO（2000 章光刷盘 14 秒）。
# 50 个一批：提交开销摊到可忽略，中途崩了最多重做一批。
COMMIT_EVERY = 50


def _chunk_payload(text: str) -> list[dict[str, Any]]:
    out = []
    for c in split_text(text):
        out.append({
            "seq": c.seq,
            "text": c.text,
            "text_tok": tokenize.tokenize(c.text),
            "start": c.start,
            "end": c.end,
        })
    return out


async def index_node(
    store, node: dict[str, Any], *, force: bool = False, commit: bool = True,
) -> int:
    """给一个节点建索引；没变就跳过。返回写入的块数（跳过时返回 -1）。

    `commit=False` 只给 `reindex_work` 用：整本重建按批提交，理由见 COMMIT_EVERY。
    """
    content = node.get("content") or ""
    if not content.strip():
        await store.drop_index_unit(
            ref_kind="node", ref_id=node["id"], node_id=node["id"], commit=commit)
        return 0

    digest = node.get("content_hash") or _sha1(content)
    if not force and await store.index_stamp("node", node["id"]) == digest:
        return -1

    payload = _chunk_payload(content)
    await store.replace_chunks(
        work_id=node["work_id"],
        ref_kind="node",
        ref_id=node["id"],
        source_kind="body",
        ordinal=int(node.get("ordinal") or 0),
        content_hash=digest,
        texts=payload,
        node_id=node["id"],
        commit=commit,
    )
    return len(payload)


async def index_source(
    store, source: dict[str, Any], *, force: bool = False, commit: bool = True,
) -> int:
    content = source.get("content") or ""
    if not content.strip():
        return 0
    digest = _sha1(content)
    if not force and await store.index_stamp("source", source["id"]) == digest:
        return -1

    payload = _chunk_payload(content)
    await store.replace_chunks(
        work_id=source["work_id"],
        ref_kind="source",
        ref_id=source["id"],
        source_kind="source",
        ordinal=0,
        content_hash=digest,
        texts=payload,
        source_id=source["id"],
        commit=commit,
    )
    return len(payload)


async def reindex_work(
    store,
    work_id: str,
    *,
    force: bool = False,
    include_sources: bool = True,
) -> dict[str, Any]:
    """重建（增量）一本书的索引。force=True 时先清空。

    提交按批走（COMMIT_EVERY）：这是派生数据的整批重算，不需要一个单元一个
    事务；中途抛错就整批回滚——把半截索引留着比没建更难受（搜到的块对应
    不到台账，下次重建还看不出哪一半缺）。
    """
    started = time.perf_counter()

    # 术语/人名先灌进 jieba 词典，否则「青云宗」会被切成「青云 / 宗」
    terms = await store.list_terms(work_id)
    if terms:
        tokenize.add_words([t["term"] for t in terms])

    if force:
        await store.clear_chunks(work_id)

    nodes_done = 0
    node_chunks = 0
    skipped = 0
    pending = 0
    try:
        for item in await store.list_readable(work_id):
            node = await store.get_node(item["id"])
            if not node:
                continue
            n = await index_node(store, node, force=force, commit=False)
            if n < 0:
                skipped += 1
                continue
            nodes_done += 1
            node_chunks += n
            pending += 1
            if pending >= COMMIT_EVERY:
                await store.commit()
                pending = 0

        sources_done = 0
        source_chunks = 0
        if include_sources:
            for s in await store.list_sources(work_id, limit=500):
                full = await store.get_source(s["id"])
                if not full:
                    continue
                n = await index_source(store, full, force=force, commit=False)
                if n < 0:
                    skipped += 1
                    continue
                sources_done += 1
                source_chunks += n
                pending += 1
                if pending >= COMMIT_EVERY:
                    await store.commit()
                    pending = 0
        await store.commit()
    except BaseException:
        await store.rollback()
        raise

    total = await store.count_chunks(work_id)
    # 摘要倒排是后加的（S5 第二路）：老库里已有的摘要没有倒排，但正文没变、
    # 增量逻辑会跳过它们。每次重建顺带回填一次，幂等，几千条只要几十毫秒。
    summaries_indexed = await store.rebuild_summaries_fts(work_id)
    await store.touch_index_state(work_id, last_chunk_ordinal=0, dirty=0)
    return {
        "nodes": nodes_done,
        "node_chunks": node_chunks,
        "sources": sources_done,
        "source_chunks": source_chunks,
        "skipped": skipped,
        "chunks_total": total,
        "summaries_indexed": summaries_indexed,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def index_status(store, work_id: str) -> dict[str, Any]:
    units = await store.list_index_units(work_id)
    body = await store.count_chunks(work_id, "body")
    src = await store.count_chunks(work_id, "source")
    readable = await store.list_readable(work_id)
    summaries = await store.list_summaries(work_id, "node")
    stale = await store.stale_summaries(work_id)
    return {
        "chunks_body": body,
        "chunks_source": src,
        "nodes_indexed": sum(1 for u in units if u["ref_kind"] == "node"),
        "sources_indexed": sum(1 for u in units if u["ref_kind"] == "source"),
        "nodes_total": len(readable),
        "summaries": len(summaries),
        # 正文改过但还没重算的：这两个计数就是右栏「重算过期摘要」按钮的依据
        "stale_summaries": sum(1 for s in stale if s["scope"] == "node"),
        "stale_upstream": sum(1 for s in stale if s["scope"] != "node"),
        "state": await store.index_state(work_id),
    }
