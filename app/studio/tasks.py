"""任务执行器：队列里每种 task_type 对应一个 async 函数。

设计约束：

1. **执行器不知道队列的存在**。它只接收 `TaskContext`（store / gateway / 作品 / 节点 /
   参数），返回一个结果 dict；重试、暂停、打分记录都由队列层负责。这样单个执行器
   可以在脚本里直接调用，不必起 worker。
2. **后处理跟在主操作后面，不在队列里排第二个任务**。「写完一节」这件事在语义上
   包含建索引、抽设定、查一致性——拆成多个 job 只会让断点恢复变复杂，而且中间
   失败会留下「正文有了、图没抽」的半成品。
3. **失败要区分可重试与不可重试**。上游风控（登录失效、滑块）重试一百次也没用，
   必须把队列整体停下来等人；只有超时、网络抖动这类才值得退避重试。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from app.core.errors import HumanVerifyRequired, NeedsLogin, ProviderBlocked, W2AError
from app.studio import consistency, graph, indexer, prompting, scoring, summarizer
from app.studio.assembler import assemble

# 这些错误说明「人不在」，重试没有意义：必须暂停整条队列
FATAL_ERRORS = (NeedsLogin, HumanVerifyRequired, ProviderBlocked)


@dataclass
class TaskContext:
    store: Any
    gateway: Any
    work: dict[str, Any]
    node: Optional[dict[str, Any]]
    payload: dict[str, Any] = field(default_factory=dict)
    resolved: dict[str, Any] = field(default_factory=dict)
    template: Optional[dict[str, Any]] = None
    model: str = ""
    mode: str = "quick"


async def _prev_summary(store, work_id: str, ordinal: int) -> str:
    # 只要 fresh：上一节刚刚改过稿时，宁可不给前情提示，也不能拿旧摘要去定节拍卡
    rows = [s for s in await store.list_summaries(work_id, "node")
            if int(s.get("ref_ordinal") or 0) < ordinal and s.get("fresh")]
    if not rows:
        return ""
    last = max(rows, key=lambda s: int(s.get("ref_ordinal") or 0))
    return (last.get("content") or "")[:300]


async def _recent_texts(store, work_id: str, ordinal: int, k: int = 5) -> list[str]:
    nodes = [n for n in await store.list_readable(work_id)
             if int(n.get("ordinal") or 0) < ordinal]
    out: list[str] = []
    for item in nodes[-k:]:
        full = await store.get_node(item["id"])
        if full and (full.get("content") or "").strip():
            out.append(full["content"])
    return out


async def _post_process(ctx: TaskContext, node: dict[str, Any], text: str) -> dict[str, Any]:
    """生成之后的固定动作：索引 → 摘要 → 抽设定 → 查一致性 → 打分。"""
    store, work = ctx.store, ctx.work
    is_novel = (work.get("kind") or "article") == "novel"
    steps: dict[str, Any] = {}

    fresh = await store.get_node(node["id"])
    if not fresh:
        return steps

    if ctx.payload.get("index", True):
        try:
            steps["chunks"] = await indexer.index_node(store, fresh)
        except Exception as exc:  # noqa: BLE001 - 后处理失败不该让正文白写
            logger.warning(f"[jobs] 索引失败 node={node['id']}: {exc}")

    if ctx.payload.get("summarize", True):
        try:
            sm = await summarizer.summarize_node(ctx.gateway, store, fresh, model=ctx.model)
            if sm:
                steps["summary"] = len(sm)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[jobs] 摘要失败 node={node['id']}: {exc}")

    if is_novel and ctx.payload.get("extract", True):
        try:
            ex = await graph.extract_node(ctx.gateway, store, ctx.work, fresh, model=ctx.model)
            steps["extract"] = {k: ex.get(k, 0) for k in
                                ("entities", "relations", "events", "foreshadows")}
            if ex.get("error"):
                steps["extract"]["error"] = ex["error"]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[jobs] 抽取失败 node={node['id']}: {exc}")

    issues: list[dict] = []
    if ctx.payload.get("check", True):
        try:
            await consistency.run_checks(store, ctx.work, fresh, resolved=ctx.resolved,
                                        template=ctx.template)
            issues = await store.list_issues(work["id"], node_id=node["id"], only_open=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[jobs] 检查失败 node={node['id']}: {exc}")

    # 打分：拿不到问题清单就按无告警算，宁可宽一点也不要因为后处理失败把好稿判成废稿
    meta = prompting.work_meta(work)
    target = prompting.target_words_of(fresh, meta, ctx.resolved)
    entities = [e["name"] for e in await store.list_entities(work["id"])] if is_novel else []
    scored = scoring.score_node(
        fresh, text, kind=work.get("kind") or "article", target_words=target,
        issues=issues, recent_texts=await _recent_texts(store, work["id"],
                                                       int(fresh.get("ordinal") or 0)),
        entity_names=entities,
    )
    steps["score"] = scored
    steps["issues"] = [{"rule": i["rule"], "severity": i["severity"],
                        "message": i["message"]} for i in issues]
    return steps


async def run_beats(ctx: TaskContext) -> dict[str, Any]:
    """章纲 / 节拍卡：先定「发生哪几件事」，正文才有骨架。"""
    node = ctx.node
    if not node:
        raise ValueError("beats 任务需要 node_id")
    meta = prompting.work_meta(ctx.work)
    focus = str(((meta.get("outline") or {}).get(node["id"]) or {}).get("prompt") or "")
    prev = await _prev_summary(ctx.store, ctx.work["id"], int(node.get("ordinal") or 0))
    # 已抽出的实体优先：第一章之前图谱为空，那就只靠 focus
    known = [e["name"] for e in await ctx.store.list_entities(ctx.work["id"])]
    system, user = prompting.beats_prompt(ctx.work, node, prev_hint=prev,
                                        focus=focus, known_names=known)
    res = await ctx.gateway.complete(
        model=ctx.model, system=system, prompt=user, mode="quick")
    beats = prompting.parse_beats(res.text)
    if not beats:
        raise ValueError("节拍卡为空")
    await ctx.store.update_node(node["id"], summary=beats)
    return {"chars": len(beats), "beats": beats}


async def run_draft(ctx: TaskContext) -> dict[str, Any]:
    """写一节正文：装配 → 生成 → 落库 → 索引/摘要/抽取/检查/打分。"""
    store, work, node = ctx.store, ctx.work, ctx.node
    if not node:
        raise ValueError("draft 任务需要 node_id")

    is_novel = (work.get("kind") or "article") == "novel"
    # 小说且没有节拍卡时先补一张：批量跑的时候没人会给每章手动写节拍
    if is_novel and ctx.payload.get("auto_beats", True) and not (node.get("summary") or "").strip():
        try:
            await run_beats(ctx)
            node = await store.get_node(node["id"]) or node
        except Exception as exc:  # noqa: BLE001 - 节拍失败也要能写出东西来
            logger.warning(f"[jobs] 自动节拍失败 node={node['id']}: {exc}")

    meta = prompting.work_meta(work)
    instruction = prompting.node_instruction(node, meta, ctx.payload.get("instruction", ""))
    task_type = "draft" if is_novel else "article_draft"
    result = await assemble(store, work, node, task_type, instruction,
                            template=ctx.template, resolved=ctx.resolved)

    provider = ctx.gateway.resolve(ctx.model)[0]
    run_id = await store.start_run(
        work_id=work["id"], node_id=node["id"], task_type=task_type,
        model=ctx.model, provider=provider, prompt_chars=result.total_chars,
    )
    started = time.perf_counter()
    parts: list[str] = []
    error = ""
    cause: Optional[BaseException] = None
    try:
        async for chunk in ctx.gateway.stream(
            model=ctx.model, system=result.system, prompt=result.user, mode=ctx.mode
        ):
            if chunk.text:
                parts.append(chunk.text)
    except Exception as exc:  # noqa: BLE001
        # 必须把异常对象留住：在 finally 里裸 `raise` 会变成
        # 「RuntimeError: No active exception to reraise」，真实的上游错误
        # （登录失效/验证码）会被掩盖，队列的致命错误判定就失效了。
        error = str(exc)
        cause = exc
    finally:
        text = "".join(parts).strip()
        elapsed = int((time.perf_counter() - started) * 1000)
        if error or not text:
            await store.finish_run(run_id, status="failed", output_chars=len(text),
                                   error=error or "模型返回空正文", elapsed_ms=elapsed)
            if cause is not None:
                raise cause
            raise ValueError("模型返回空正文")

    await store.update_node(node["id"], content=text, snapshot=True)
    steps = await _post_process(ctx, node, text)
    score = (steps.get("score") or {}).get("score")
    await store.finish_run(run_id, status="ok", output_chars=len(text), elapsed_ms=elapsed,
                           score=score)
    return {
        "chars": len(text),
        "elapsed_ms": elapsed,
        "score": score,
        "grade": (steps.get("score") or {}).get("grade"),
        "notes": (steps.get("score") or {}).get("notes") or [],
        "issues": steps.get("issues") or [],
        "buckets": [b.as_dict() for b in result.buckets],
        "warnings": result.warnings,
    }


async def run_polish(ctx: TaskContext) -> dict[str, Any]:
    """润色已有正文：只改语言，不改事实与结构。"""
    store, work, node = ctx.store, ctx.work, ctx.node
    if not node or not (node.get("content") or "").strip():
        raise ValueError("polish 任务需要已有正文")
    instruction = ctx.payload.get("instruction") or "只做语言层面的修改：错别字、病句、重复、标点；不改事实、结构与顺序。"
    result = await assemble(store, work, node, "polish", instruction,
                            template=ctx.template, resolved=ctx.resolved)
    res = await ctx.gateway.complete(model=ctx.model, system=result.system,
                                    prompt=result.user, mode=ctx.mode)
    text = (res.text or "").strip()
    if not text:
        raise ValueError("润色结果为空")
    await store.update_node(node["id"], content=text, snapshot=True)
    steps = await _post_process(ctx, node, text)
    return {"chars": len(text), "elapsed_ms": res.elapsed_ms,
            "score": (steps.get("score") or {}).get("score"),
            "issues": steps.get("issues") or []}


async def run_check(ctx: TaskContext) -> dict[str, Any]:
    """只跑一致性检查并重算分数（不调模型）。"""
    node = ctx.node
    if not node:
        raise ValueError("check 任务需要 node_id")
    await consistency.run_checks(ctx.store, ctx.work, node, resolved=ctx.resolved,
                                template=ctx.template)
    issues = await ctx.store.list_issues(ctx.work["id"], node_id=node["id"], only_open=True)
    content = node.get("content") or ""
    meta = prompting.work_meta(ctx.work)
    scored = scoring.score_node(
        node, content, kind=ctx.work.get("kind") or "article",
        target_words=prompting.target_words_of(node, meta, ctx.resolved),
        issues=issues,
        recent_texts=await _recent_texts(ctx.store, ctx.work["id"],
                                        int(node.get("ordinal") or 0)),
        entity_names=[e["name"] for e in await ctx.store.list_entities(ctx.work["id"])],
    )
    return {"issues": len(issues), "score": scored["score"], "grade": scored["grade"],
            "notes": scored["notes"]}


async def run_summarize(ctx: TaskContext) -> dict[str, Any]:
    node = ctx.node
    if not node:
        raise ValueError("summarize 任务需要 node_id")
    text = await summarizer.summarize_node(ctx.gateway, ctx.store, node,
                                          model=ctx.model, force=bool(ctx.payload.get("force")))
    if not text:
        raise ValueError("正文太短或摘要失败")
    return {"chars": len(text)}


async def run_extract(ctx: TaskContext) -> dict[str, Any]:
    node = ctx.node
    if not node:
        raise ValueError("extract 任务需要 node_id")
    res = await graph.extract_node(ctx.gateway, ctx.store, ctx.work, node,
                                  model=ctx.model, force=bool(ctx.payload.get("force")))
    if res.get("error"):
        raise ValueError(res["error"])
    return {k: res.get(k, 0) for k in ("entities", "relations", "events", "states", "foreshadows")}


REGISTRY = {
    "beats": run_beats,
    "draft": run_draft,
    "polish": run_polish,
    "check": run_check,
    "summarize": run_summarize,
    "extract": run_extract,
}


async def run_task(ctx: TaskContext, task_type: str) -> dict[str, Any]:
    runner = REGISTRY.get(task_type)
    if not runner:
        raise ValueError(f"未知任务类型: {task_type}")
    return await runner(ctx)


def is_fatal(exc: BaseException) -> bool:
    """判断这个异常是不是「人不在」级别：登录失效、滑块、账号受限。"""
    if isinstance(exc, FATAL_ERRORS):
        return True
    if isinstance(exc, W2AError):
        code = (getattr(exc, "code", "") or "").lower()
        text = str(exc).lower()
        return any(k in text or k in code for k in
                   ("login", "verify", "challenge", "blocked", "风控", "滑块", "登录",
                    "禁言", "封禁", "账号受限"))
    return False


def payload_of(job: dict[str, Any]) -> dict[str, Any]:
    try:
        got = json.loads(job.get("payload_json") or "{}")
        return got if isinstance(got, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
