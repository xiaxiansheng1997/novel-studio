"""分层摘要：节 → 讲/卷 → 全书。

为什么必须有三段式（事件 / 变化 / 钩子）而不是「用一句话概括」：
- 事件：给下一节的上下文，回答「前面发生了什么」
- 变化：给图谱与一致性检查用（人物状态、物品归属、地点），是结构化信息的雏形
- 钩子：给伏笔表用，未回收的钩子在第 N 节应该被提起

一次调用服务三个下游，比分别再调三次便宜得多。

分层：节摘要 → 容器（讲/卷）摘要 → 全书概要。
到 20 万字时只有节摘要会在上下文中「找不到第 7 讲讲什么」，中间层就是给这个用的。

过期判定走内容指纹（`summaries.ref_hash`），跟 `graph.extract_node` 用 `index_stamp` 是同一个套路。
之前这里比的是序号：序号相等就认为摘要还能用，于是改完第 7 章重跑队列，拿回来的还是上一版的摘要，
而 S4「最近节摘要」会拿着这份旧记忆去写第 8 章。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from loguru import logger

# 口径必须与 `nodes.content_hash` 完全一致（同样 sha1 取前 16 位，且不 strip）。
# 自己另写一个或者先 strip 再算，会让每一节都被判过期，等于每次都在重跑摘要。
from app.studio.store import _hash

SUMMARY_SYSTEM = (
    "你是中文写作的编辑助理。你的任务是把一节正文压缩成可复用的记忆卡。"
    "只输出下面三行，不要开场白、不要总结、不要评价。"
)

SUMMARY_USER = """读下面这一节，输出三行，格式严格如下：
事件：<不超过 80 字，写清了发生什么>
变化：<最多 3 条，每条一行，用「- 」开头；没有就写「- 无」>
钩子：<最多 3 条，每条一行，用「- 」开头；没有就写「- 无」>

<section title="{title}">
{content}
</section>"""

MERGE_SYSTEM = (
    "你是中文写作的编辑助理。把多节摘要合并成更高一层的记忆卡，"
    "保留贯穿性的事件、状态变化与未回收的钩子。只输出正文，不要解释。"
)

MERGE_USER = """把下面这些节的摘要合并成一份「{label}摘要」，300 字以内。
必须保留：①主线事件 ②仍然有效的状态变化 ③尚未回收的钩子。
不要逐节罗列，要合并同类项。

{parts}"""

# 摘要输入上限：单节正文超过这个长度只取尾部（结尾才是「结果」）
MAX_INPUT_CHARS = 6000


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _clean(text: str) -> str:
    raw = (text or "").strip()
    # 模型偶尔会包代码块或加标题，去掉最外层噪声
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = re.sub(r"^(摘要|记忆卡)[:：]\s*", "", raw)
    return raw.strip()


async def summarize_node(
    gateway,
    store,
    node: dict[str, Any],
    *,
    model: str,
    force: bool = False,
    min_chars: int = 200,
) -> Optional[str]:
    """给一个节点生成摘要并落库。返回摘要文本（跳过时返回 None）。"""
    content_raw = node.get("content") or ""
    content = content_raw.strip()
    if len(content) < min_chars:
        return None
    # 指纹按未 strip 的原文算，才能和 update_node 写下的 content_hash 对上
    digest = _hash(content_raw)
    if not force:
        exist = await store.get_summary("node", node["id"])
        if exist and (exist.get("ref_hash") or "") == digest:
            return exist["content"]

    snippet = _tail(content, MAX_INPUT_CHARS)
    try:
        res = await gateway.complete(
            model=model,
            system=SUMMARY_SYSTEM,
            prompt=SUMMARY_USER.format(title=node.get("title") or "", content=snippet),
            mode="quick",
        )
    except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响主流程
        logger.warning(f"[studio] 摘要生成失败 node={node['id']}: {exc}")
        return None

    summary = _clean(res.text)
    if not summary:
        return None
    await store.upsert_summary(
        node["work_id"],
        scope="node",
        ref_id=node["id"],
        content=summary,
        ref_ordinal=int(node.get("ordinal") or 0),
        model=model,
        ref_hash=digest,
    )
    return summary


async def summarize_container(
    gateway,
    store,
    work_id: str,
    container: dict[str, Any],
    *,
    model: str,
    label: str = "讲",
    force: bool = False,
) -> Optional[str]:
    """把一个容器（讲/卷）下所有节摘要合并成上层摘要。"""
    if not force:
        exist = await store.get_summary("container", container["id"])
        # 容器摘要没有单一源文本：只要 ref_hash 非空就认它是当前这批子摘要拼出来的。
        # 子节正文一改，store._mark_stale_locked 会把它置空，于是这里自然重算。
        # （summarize_work_batch 按 nodes → containers → work 的顺序跑，轮到这里时子摘要已经是新的。）
        if exist and (exist.get("ref_hash") or ""):
            return exist["content"]

    tree = await store.list_tree(work_id)
    child_ids = [n["id"] for n in tree if n.get("parent_id") == container["id"]]
    if not child_ids:
        return None
    summaries = await store.list_summaries(work_id, "node")
    by_id = {s["ref_id"]: s["content"] for s in summaries}
    parts = [f"【{i}】{by_id[cid]}" for i, cid in enumerate(child_ids, 1) if cid in by_id]
    if not parts:
        return None
    merged_src = "\n\n".join(parts)

    try:
        res = await gateway.complete(
            model=model,
            system=MERGE_SYSTEM,
            prompt=MERGE_USER.format(label=label, parts=merged_src),
            mode="quick",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[studio] 容器摘要失败 node={container['id']}: {exc}")
        return None

    text = _clean(res.text)
    if not text:
        return None
    await store.upsert_summary(
        work_id, scope="container", ref_id=container["id"], content=text,
        ref_ordinal=int(container.get("ordinal") or 0), model=model,
        ref_hash=_hash(merged_src),
    )
    return text


async def summarize_work(
    gateway, store, work_id: str, *, model: str, title: str = "", force: bool = False
) -> Optional[str]:
    """全书概要：合并容器摘要（没有容器就用节摘要）。"""
    if not force:
        exist = await store.get_summary("work", work_id)
        if exist and (exist.get("ref_hash") or ""):
            return exist["content"]

    containers = await store.list_summaries(work_id, "container")
    parts_src = [s["content"] for s in containers if s.get("content")]
    if not parts_src:
        nodes = await store.list_summaries(work_id, "node")
        parts_src = [s["content"] for s in nodes if s.get("content")]
    if not parts_src:
        return None
    merged_src = "\n\n".join(parts_src)

    try:
        res = await gateway.complete(
            model=model,
            system=MERGE_SYSTEM,
            prompt=MERGE_USER.format(label="全书", parts=merged_src),
            mode="quick",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[studio] 全书概要失败 work={work_id}: {exc}")
        return None

    text = _clean(res.text)
    if not text:
        return None
    await store.upsert_summary(work_id, scope="work", ref_id=work_id, content=text,
                               model=model, ref_hash=_hash(merged_src))
    return text


async def summarize_work_batch(
    gateway, store, work_id: str, *, model: str, scope: str = "nodes", force: bool = False
) -> dict[str, Any]:
    """批量生成：nodes → containers → work。scope 控制只做哪一层。"""
    done = {"nodes": 0, "containers": 0, "work": 0}
    if scope in ("nodes", "all"):
        for item in await store.list_readable(work_id):
            node = await store.get_node(item["id"])
            if not node:
                continue
            if await summarize_node(gateway, store, node, model=model, force=force):
                done["nodes"] += 1
    if scope in ("containers", "all"):
        for n in await store.list_tree(work_id):
            if n.get("ordinal") is None and int(n.get("depth") or 0) == 0:
                node = await store.get_node(n["id"])
                if node and await summarize_container(gateway, store, work_id, node,
                                                      model=model, force=force):
                    done["containers"] += 1
    if scope in ("work", "all"):
        if await summarize_work(gateway, store, work_id, model=model, force=force):
            done["work"] = 1
    return done
