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
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from loguru import logger

from app.core.errors import HumanVerifyRequired, NeedsLogin, ProviderBlocked, W2AError
from app.studio import (aivoice, consistency, content_types, craft, graph, indexer,
                        prompting, sampling, scoring, summarizer)
from app.studio.assembler import assemble, record_used_sources
from app.studio.proofread import PARA_SPLIT, _paragraphs

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
    focus = prompting.node_outline(node, meta).get("prompt") or ""
    prev = await _prev_summary(ctx.store, ctx.work["id"], int(node.get("ordinal") or 0))
    # 已抽出的实体优先：第一章之前图谱为空，那就只靠 focus
    known = [e["name"] for e in await ctx.store.list_entities(ctx.work["id"])]
    system, user = prompting.beats_prompt(ctx.work, node, prev_hint=prev,
                                        focus=focus, known_names=known)
    res = await ctx.gateway.complete(
        model=ctx.model, system=system, prompt=user, mode="quick",
        **sampling.for_task("beats"))
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
    await record_used_sources(store, result)

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
            model=ctx.model, system=result.system, prompt=result.user, mode=ctx.mode,
            **sampling.for_task(task_type)
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

    await store.update_node(node["id"], content=text, snapshot=True,
                            snapshot_source="ai_draft", run_id=run_id)
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
    await record_used_sources(store, result)
    res = await ctx.gateway.complete(model=ctx.model, system=result.system,
                                    prompt=result.user, mode=ctx.mode,
                                    **sampling.for_task("polish"))
    text = (res.text or "").strip()
    if not text:
        raise ValueError("润色结果为空")
    # 润色写的也是正文，来源必须标 ai_polish：历史页里分不出「AI 改的」和「我改的」就不敢回滚
    await store.update_node(node["id"], content=text, snapshot=True, snapshot_source="ai_polish")
    steps = await _post_process(ctx, node, text)
    return {"chars": len(text), "elapsed_ms": res.elapsed_ms,
            "score": (steps.get("score") or {}).get("score"),
            "issues": steps.get("issues") or []}


# deai 一次最多改这么多段：再多的话 prompt 会比原文还长，而且模型改到后面
# 几段时已经开始走神（同一批里前几段的质量明显高于后几段）。
DEAI_MAX_PARAS = 6
# 短于这个字数的段落改长度校验改用绝对容差：一句「她转身。」扩成一句描写
# 是合法的，比例却早就超线了。
DEAI_SHORT_PARA = 40


def _deai_targets(content: str, hits: list[dict]) -> list[tuple[int, str]]:
    """把命中清单还原成「要改哪几段」。命中条数多的段优先，最后按段号排。"""
    by_idx = dict(_paragraphs(content))
    weight: dict[int, int] = {}
    for hit in hits:
        rule = hit.get("rule") or ""
        idxs = aivoice.paragraphs_with(content, rule)
        loc = str(hit.get("location") or "").strip()
        if loc.isdigit():
            idxs = idxs + [int(loc)]
        for idx in idxs:
            if idx in by_idx:
                weight[idx] = weight.get(idx, 0) + 1
    ranked = sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))[:DEAI_MAX_PARAS]
    return [(idx, by_idx[idx]) for idx in sorted(k for k, _ in ranked)]


def _para_spans(content: str) -> dict[int, tuple[int, int]]:
    """段号 → (起点, 终点)，口径与 `proofread._paragraphs` 完全一致。

    贴回必须按区间改，不能按 text 找：`content.find(old)` 只保证「找到一段
    一样的文字」，不保证是第 idx 段。目标段之前若有一模一样（或包含它）的
    段落，找到的是前一处——用户没让改的段被静默改写，还报「已改 1 段」。
    而 `_deai_targets` 只挑权重最高的 6 段，「重复段里只选中后一处」是常态。
    """
    spans: dict[int, tuple[int, int]] = {}
    bounds: list[tuple[int, int]] = []
    start = 0
    for m in PARA_SPLIT.finditer(content):
        bounds.append((start, m.start()))
        start = m.end()
    bounds.append((start, len(content)))
    idx = 0
    for lo, hi in bounds:
        block = content[lo:hi]
        body = block.strip()
        if not body:
            continue
        idx += 1
        head = lo + len(block) - len(block.lstrip())
        spans[idx] = (head, head + len(body))
    return spans


def _deai_size_ok(old: str, new: str) -> bool:
    """长度失控就拒。短段落不比比例：5 字的句子扩到 13 字是合法改写，
    比例 2.6 却会超线——误拒比放过安全，但拒光了整次任务就白跑了。"""
    if len(old) < DEAI_SHORT_PARA:
        return len(new) <= len(old) * 4 + 20
    return 0.4 <= len(new) / len(old) <= 2.0


def _deai_apply(content: str, rewrites: dict[int, str],
                by_idx: dict[int, str]) -> tuple[str, list[int], list[int], list[int]]:
    """把改写贴回原文。返回（新正文，换掉的段号，被拒的段号，照抄没动的段号）。

    按段号取字符区间替换，并用 delta 修正前面段落改短改长带来的偏移。
    替换前先核对区间里的原文是否就是这一段：段号口径若漂移（例如正文里有
    代码块，`check_node` 是在去代码后的文本上算的段号），这里会拒而不是改错。

    拆段与长度失控一律拒绝：改坏一段比不改更糟，而且它不报错。
    """
    out = content
    spans = _para_spans(content)
    delta = 0
    done: list[int] = []
    rejected: list[int] = []
    same: list[int] = []
    for idx in sorted(rewrites):
        old = by_idx.get(idx) or ""
        new = (rewrites[idx] or "").strip()
        if not old or not new:
            continue
        if new == old:
            # 模型原样抄回：不是改坏，但也不能算改成。混进 rejected 会让
            # 「全被拒」和「全照抄」两个完全不同的原因落进同一句报错。
            same.append(idx)
            continue
        span = spans.get(idx)
        if not span:
            rejected.append(idx)
            continue
        start, end = span[0] + delta, span[1] + delta
        if out[start:end] != old or "\n" in new or not _deai_size_ok(old, new):
            rejected.append(idx)
            continue
        out = out[:start] + new + out[end:]
        delta += len(new) - (end - start)
        done.append(idx)
    return out, done, rejected, same


async def run_deai(ctx: TaskContext) -> dict[str, Any]:
    """去 AI 味：只重写命中的那几段，情节与对白内容一概不动。

    为什么不做整篇润色：`polish` 的指令是「只改错别字病句」，模型看到
    「去 AI 味」这四个字会顺手把情节也改了；按段定位改写，改坏的范围
    最多是一段，回滚成本低，也让人能逐段看改得对不对。
    """
    store, work, node = ctx.store, ctx.work, ctx.node
    if not node or not (node.get("content") or "").strip():
        raise ValueError("deai 任务需要已有正文")
    if (work.get("kind") or "article") != "novel":
        raise ValueError("deai 只用于小说")

    source = node["content"]
    hits = aivoice.check_node(source, int(node.get("ordinal") or 0))
    if not hits:
        return {"chars": len(source), "rewritten": 0, "hits": [],
                "skipped": "没有命中任何 AI 味规则"}

    targets = _deai_targets(source, hits)
    if not targets:
        return {"chars": len(source), "rewritten": 0, "hits": hits,
                "skipped": "命中的问题需要对白层面的改动，逐段改写做不到"}

    requirements = content_types.requirements_text(ctx.resolved)
    system, user = prompting.deai_prompt(targets, hits, requirements)
    run_id = await store.start_run(
        work_id=work["id"], node_id=node["id"], task_type="deai",
        model=ctx.model, provider=ctx.gateway.resolve(ctx.model)[0],
        prompt_chars=len(system) + len(user),
    )
    started = time.perf_counter()
    try:
        res = await ctx.gateway.complete(model=ctx.model, system=system, prompt=user,
                                        mode=ctx.mode, **sampling.for_task("deai"))
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.perf_counter() - started) * 1000)
        await store.finish_run(run_id, status="failed", error=str(exc), elapsed_ms=elapsed)
        raise
    elapsed = int((time.perf_counter() - started) * 1000)

    rewrites = prompting.parse_deai(res.text)
    text, done, rejected, same = _deai_apply(source, rewrites, dict(targets))
    if not done:
        # 三种失败原因要分开报：都写成「段号对不上或长度异常」时，看日志的人
        # 分不清是该改提示词（全照抄）还是该查粘贴逻辑（段号对不上）。
        if same and not rejected:
            why = f"模型把 {len(same)} 段原文照抄了回来，没有改写"
        elif rejected:
            why = (f"改写结果无法贴回原文：{len(rejected)} 段被拒"
                   f"（段号对不上、拆成了多段，或长度偏离过大）")
        else:
            why = "模型没有按【段号】格式返回改写结果"
        await store.finish_run(run_id, status="failed", error=why,
                               output_chars=len(res.text or ""), elapsed_ms=elapsed)
        raise ValueError(why)

    await store.update_node(node["id"], content=text, snapshot=True,
                            snapshot_source="ai_deai", run_id=run_id)
    # 情节没动，重新抽设定纯属浪费；索引、摘要、检查、打分都要重跑
    light = replace(ctx, payload={**ctx.payload, "extract": False})
    steps = await _post_process(light, node, text)
    await store.finish_run(run_id, status="ok", output_chars=len(text), elapsed_ms=elapsed,
                           score=(steps.get("score") or {}).get("score"))
    return {"chars": len(text), "rewritten": len(done), "paragraphs": done,
            "rejected": rejected, "untouched": same, "hits": hits,
            "elapsed_ms": elapsed, "issues": steps.get("issues") or []}


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
    "deai": run_deai,
    "check": run_check,
    "summarize": run_summarize,
    "extract": run_extract,
    # 手艺拆解：章/卷/书三级共用一个 task_type，层级在 payload.level 里分叉
    "craft": craft.run_craft,
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
