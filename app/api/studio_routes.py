"""写作工作台 API：/studio/*。

覆盖三条主链路：
1. 作品 / 节点树 / 单节生成与保存
2. 单篇与系列：「大纲 → 逐节 → 拼装导出」全自动流水线
3. 加工类任务（改写/压缩/扩写/润色/翻译/拆分/合并）+ 多候选
记忆（S3/S4/S5）、图谱（S6）与一致性检查已接入；任务队列在 M4。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from app.core.auth import verify_key
from app.llm.gateway import Gateway, pick_model
from app.studio import content_types, consistency, graph, indexer, retrieval, summarizer
from app.studio.assembler import assemble
from app.studio.jsonutil import parse_json_loose as _parse_json_loose
from app.studio.prompting import node_instruction as _node_instruction
from app.studio.store import BackupError

router = APIRouter()


# ==================== 依赖 ====================

def _store(request: Request):
    return request.app.state.studio_store


def _gateway(request: Request) -> Gateway:
    return request.app.state.gateway


async def _require_work(request: Request, work_id: str) -> dict[str, Any]:
    work = await _store(request).get_work(work_id)
    if not work:
        raise HTTPException(status_code=404, detail=f"作品不存在: {work_id}")
    return work


# ==================== 请求模型 ====================

class WorkCreate(BaseModel):
    kind: Literal["novel", "article"] = "article"
    scale: Literal["single", "series"] = "single"
    subtype: str = ""
    title: str = Field(min_length=1, max_length=120)
    premise: str = ""
    template_id: Optional[str] = None
    target_words: int = 0


class WorkUpdate(BaseModel):
    title: Optional[str] = None
    premise: Optional[str] = None
    status: Optional[str] = None
    scale: Optional[str] = None
    subtype: Optional[str] = None
    template_id: Optional[str] = None
    target_words: Optional[int] = None
    style_json: Optional[str] = None
    meta_json: Optional[str] = None


class NodeCreate(BaseModel):
    kind: Literal["volume", "chapter", "section"] = "section"
    title: str = Field(min_length=1, max_length=120)
    parent_id: Optional[str] = None
    content: str = ""


class NodeUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    # 前端点「保存」时传 true，才把当前正文留一份版本；自动保存不传
    snapshot: bool = False


class NodeMove(BaseModel):
    # 同层上/下移：只与紧邻的前/后兄弟互换，不支持跨父搬动
    direction: Literal["up", "down"]


class SourceCreate(BaseModel):
    title: str = ""
    url: str = ""
    kind: str = "note"
    content: str = Field(min_length=1)
    published_at: str = ""


class FactCreate(BaseModel):
    statement: str = Field(min_length=1)
    source_id: Optional[str] = None
    confidence: float = 1.0
    valid_until: str = ""


class TermCreate(BaseModel):
    term: str = Field(min_length=1)
    definition: str = ""
    preferred: str = ""
    forbidden: list[str] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    task_type: str = "article_draft"
    instruction: str = ""
    model: str = ""
    mode: str = "quick"
    # 生成结果是否直接写入节点正文（续写=追加，草稿/润色=替换）
    save: bool = False
    # 多候选时标记这是第几个（0 起），写入 runs.variant 方便事后比较
    variant: int = 0


# ==================== 模板 ====================

@router.get("/studio/templates", dependencies=[Depends(verify_key)])
async def list_templates(request: Request, kind: str = "", subtype: str = ""):
    return await _store(request).list_templates(kind=kind, subtype=subtype)


# ==================== 作品 ====================

@router.get("/studio/works", dependencies=[Depends(verify_key)])
async def list_works(request: Request, kind: str = "", status: str = "", limit: int = 100):
    return await _store(request).list_works(kind=kind, status=status, limit=limit)


@router.post("/studio/works", dependencies=[Depends(verify_key)])
async def create_work(body: WorkCreate, request: Request):
    store = _store(request)
    template_id = body.template_id
    # 没指定模板时按 subtype 找一个同类的，避免单篇生成时完全没有骨架
    if not template_id and body.kind == "article":
        candidates = await store.list_templates(kind="article", subtype=body.subtype or "general")
        if not candidates:
            candidates = await store.list_templates(kind="article")
        if candidates:
            template_id = candidates[0]["id"]
    return await store.create_work(
        kind=body.kind,
        scale=body.scale,
        subtype=body.subtype,
        title=body.title,
        premise=body.premise,
        template_id=template_id,
        target_words=body.target_words,
    )


@router.get("/studio/works/{work_id}", dependencies=[Depends(verify_key)])
async def get_work(work_id: str, request: Request):
    return await _require_work(request, work_id)


@router.patch("/studio/works/{work_id}", dependencies=[Depends(verify_key)])
async def update_work(work_id: str, body: WorkUpdate, request: Request):
    await _require_work(request, work_id)
    ok = await _store(request).update_work(work_id, **body.model_dump(exclude_none=True))
    if not ok:
        raise HTTPException(status_code=400, detail="没有可更新字段")
    return await _store(request).get_work(work_id)


@router.delete("/studio/works/{work_id}", dependencies=[Depends(verify_key)])
async def delete_work(work_id: str, request: Request):
    await _require_work(request, work_id)
    try:
        await _store(request).delete_work(work_id)
    except BackupError as exc:
        # 整库副本没写成就不删：宁可不删，不能删了找不回
        raise HTTPException(status_code=500, detail=f"备份失败，已拒绝删除：{exc}") from exc
    return {"ok": True}


@router.get("/studio/works/{work_id}/tree", dependencies=[Depends(verify_key)])
async def get_tree(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await _store(request).list_tree(work_id)


@router.get("/studio/works/{work_id}/stats", dependencies=[Depends(verify_key)])
async def get_stats(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await _store(request).stats(work_id)


# ==================== 节点 ====================

@router.post("/studio/works/{work_id}/nodes", dependencies=[Depends(verify_key)])
async def create_node(work_id: str, body: NodeCreate, request: Request):
    await _require_work(request, work_id)
    try:
        return await _store(request).create_node(
            work_id=work_id,
            kind=body.kind,
            title=body.title,
            parent_id=body.parent_id,
            content=body.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/studio/nodes/{node_id}", dependencies=[Depends(verify_key)])
async def get_node(node_id: str, request: Request):
    node = await _store(request).get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    return node


@router.patch("/studio/nodes/{node_id}", dependencies=[Depends(verify_key)])
async def update_node(node_id: str, body: NodeUpdate, request: Request):
    payload = body.model_dump(exclude_none=True)
    snapshot = bool(payload.pop("snapshot", False))
    ok = await _store(request).update_node(node_id, snapshot=snapshot, **payload)
    if not ok:
        raise HTTPException(status_code=404, detail="节点不存在或无更新")
    return await _store(request).get_node(node_id)


@router.post("/studio/nodes/{node_id}/move", dependencies=[Depends(verify_key)])
async def move_node(node_id: str, body: NodeMove, request: Request):
    """同层上下移一节，与返回体里 `swapped_with_title` 那一节互换位置。

    不报 404 也不报 400：「已到边界」是正常分支（树上的首/末行点了上下移），
    前端按 `moved` 判断，把 `reason` 当提示语显示就行。
    """
    return await _store(request).move_node(node_id, body.direction)


@router.delete("/studio/nodes/{node_id}", dependencies=[Depends(verify_key)])
async def delete_node(node_id: str, request: Request):
    try:
        ok = await _store(request).delete_node(node_id)
    except BackupError as exc:
        raise HTTPException(status_code=500, detail=f"备份失败，已拒绝删除：{exc}") from exc
    if not ok:
        raise HTTPException(status_code=404, detail="节点不存在")
    return {"ok": True}


@router.get("/studio/nodes/{node_id}/versions", dependencies=[Depends(verify_key)])
async def list_versions(node_id: str, request: Request, limit: int = 30):
    return await _store(request).list_versions(node_id, limit=limit)


@router.get("/studio/nodes/{node_id}/versions/{version_id}", dependencies=[Depends(verify_key)])
async def get_version(node_id: str, version_id: str, request: Request):
    """读回一个版本的全文：列表只给字数，对比与回滚都得先拿到正文。"""
    row = await _store(request).get_version(version_id)
    if not row or row["node_id"] != node_id:
        raise HTTPException(status_code=404, detail="版本不存在")
    return row


@router.post("/studio/nodes/{node_id}/versions/{version_id}/restore",
             dependencies=[Depends(verify_key)])
async def restore_version(node_id: str, version_id: str, request: Request):
    """回滚到某个历史版本，返回回滚后的节点。

    服务端会把「当下这份还没进版本表的正文」先存一版再写回，所以前端
    必须在点之前把编辑器里的改动 flush 落库，不然那几秒的未保存内容依然会没。
    """
    node = await _store(request).restore_version(node_id, version_id)
    if node is None:
        raise HTTPException(status_code=404, detail="版本不存在或不属于该节点")
    return node


# ==================== 删除备份 ====================

@router.get("/studio/backups", dependencies=[Depends(verify_key)])
async def list_backups(request: Request):
    """列出删除前的备份：节点 JSON 与整库副本。只读。"""
    store = _store(request)
    return {
        "nodes": store.list_node_backups(),
        "works": store.list_work_backups(),
        "dir": str(store.backup_dir("nodes")),
    }


@router.post("/studio/backups/{filename}/restore", dependencies=[Depends(verify_key)])
async def restore_backup_node(filename: str, request: Request):
    """从一份节点备份找回那一节（连子树与历史版本）。

    恢复后的节挂在原父节点下（父节点没了就挂作品根），`ordinal` 追到末尾：
    要挪回原来的位置用上一节刚做完的上下移。本轮不做恢复 UI，入口就这两个接口
    与 `scripts/restore_backup.py`。
    """
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="备份文件名不合法")
    out = await _store(request).restore_node_backup(filename)
    if out is None:
        raise HTTPException(status_code=404, detail="备份不存在、读不懂，或那本书已整本删除")
    return out


# ==================== 素材 / 事实 / 术语 ====================

@router.get("/studio/works/{work_id}/sources", dependencies=[Depends(verify_key)])
async def list_sources(work_id: str, request: Request, limit: int = 100):
    await _require_work(request, work_id)
    return await _store(request).list_sources(work_id, limit=limit)


@router.post("/studio/works/{work_id}/sources", dependencies=[Depends(verify_key)])
async def add_source(work_id: str, body: SourceCreate, request: Request):
    await _require_work(request, work_id)
    return await _store(request).add_source(
        work_id,
        content=body.content,
        title=body.title,
        url=body.url,
        kind=body.kind,
        published_at=body.published_at,
    )


@router.delete("/studio/sources/{source_id}", dependencies=[Depends(verify_key)])
async def delete_source(source_id: str, request: Request):
    ok = await _store(request).delete_source(source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="素材不存在")
    return {"ok": True}


@router.get("/studio/works/{work_id}/facts", dependencies=[Depends(verify_key)])
async def list_facts(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await _store(request).list_facts(work_id)


@router.post("/studio/works/{work_id}/facts", dependencies=[Depends(verify_key)])
async def add_fact(work_id: str, body: FactCreate, request: Request):
    await _require_work(request, work_id)
    return await _store(request).add_fact(
        work_id,
        statement=body.statement,
        source_id=body.source_id,
        confidence=body.confidence,
        valid_until=body.valid_until,
    )


@router.delete("/studio/facts/{fact_id}", dependencies=[Depends(verify_key)])
async def delete_fact(fact_id: str, request: Request):
    ok = await _store(request).delete_fact(fact_id)
    if not ok:
        raise HTTPException(status_code=404, detail="事实不存在")
    return {"ok": True}


@router.get("/studio/works/{work_id}/terms", dependencies=[Depends(verify_key)])
async def list_terms(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await _store(request).list_terms(work_id)


@router.post("/studio/works/{work_id}/terms", dependencies=[Depends(verify_key)])
async def upsert_term(work_id: str, body: TermCreate, request: Request):
    await _require_work(request, work_id)
    return await _store(request).upsert_term(
        work_id,
        term=body.term,
        definition=body.definition,
        preferred=body.preferred,
        forbidden=body.forbidden,
    )


@router.delete("/studio/terms/{term_id}", dependencies=[Depends(verify_key)])
async def delete_term(term_id: str, request: Request):
    ok = await _store(request).delete_term(term_id)
    if not ok:
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"ok": True}


@router.get("/studio/works/{work_id}/issues", dependencies=[Depends(verify_key)])
async def list_issues(work_id: str, request: Request, node_id: str = "", only_open: bool = True):
    await _require_work(request, work_id)
    return await _store(request).list_issues(work_id, node_id=node_id, only_open=only_open)


# ==================== 上下文预览（调试核心） ====================

@router.post("/studio/context/preview", dependencies=[Depends(verify_key)])
async def preview_context(body: GenerateRequest, request: Request, work_id: str, node_id: str = ""):
    """只装配不调用 LLM。调 prompt 时先看这里，能省掉大量盲试。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    node = await store.get_node(node_id) if node_id else None
    if node_id and not node:
        raise HTTPException(status_code=404, detail="节点不存在")

    template = None
    if work.get("template_id"):
        tpls = [t for t in await store.list_templates() if t["id"] == work["template_id"]]
        template = tpls[0] if tpls else None
    resolved = content_types.resolve(work.get("kind") or "article",
                                    work.get("scale") or "", work.get("subtype") or "")

    result = await assemble(
        store, work, node, body.task_type, body.instruction, template=template, resolved=resolved
    )
    payload = result.as_dict()
    # 把「为什么引用了这几段」一并带上，调 prompt 时不用再去翻库
    query = retrieval.build_query(
        node.get("title") if node else "", body.instruction, work.get("premise")
    )
    payload["query"] = query
    payload["evidence"] = await retrieval.retrieve(
        store,
        work_id,
        query=query,
        current_ordinal=int((node or {}).get("ordinal") or 0) or None,
        kinds=("body",),
        top_k=8,
        budget_chars=3000,
        exclude_node_ids={node["id"]} if node else None,
    )
    return payload


# ==================== 生成（SSE） ====================

def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/studio/nodes/{node_id}/generate", dependencies=[Depends(verify_key)])
async def generate(node_id: str, body: GenerateRequest, request: Request):
    """生成正文，边生成边推 SSE。

    事件序列：meta → delta* → done（或 error）。前端拿到 delta 就能实时显示，
    3000 字大约 24 秒（M0 实测 140 字/秒），没有流式反馈体验会很差。
    """
    store = _store(request)
    node = await store.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    work = await store.get_work(node["work_id"])
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")

    template = None
    if work.get("template_id"):
        tpls = [t for t in await store.list_templates() if t["id"] == work["template_id"]]
        template = tpls[0] if tpls else None
    resolved = content_types.resolve(work.get("kind") or "article",
                                    work.get("scale") or "", work.get("subtype") or "")

    # 单节生成前把本节最新正文入索引：用户刚手改过，不重建的话
    # S5 检索到的还是旧版本，续写就会接不上。
    try:
        await indexer.index_node(store, node)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[studio] 生成前索引失败 node={node_id}: {exc}")

    result = await assemble(
        store, work, node, body.task_type, body.instruction, template=template, resolved=resolved
    )
    if not result.user.strip():
        raise HTTPException(status_code=400, detail="上下文为空：请先填写作品主旨或节点正文")

    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    run_id = await store.start_run(
        work_id=work["id"],
        node_id=node_id,
        task_type=body.task_type,
        model=model,
        provider=gateway.resolve(model)[0],
        prompt_chars=result.total_chars,
        variant=body.variant,
    )

    async def event_stream():
        started = time.perf_counter()
        parts: list[str] = []
        error = ""
        yield _sse({
            "event": "meta",
            "run_id": run_id,
            "model": model,
            "task_type": body.task_type,
            "buckets": [b.as_dict() for b in result.buckets],
            "warnings": result.warnings,
            "total_chars": result.total_chars,
        })
        try:
            async for chunk in gateway.stream(
                model=model,
                system=result.system,
                prompt=result.user,
                mode=body.mode or "quick",
            ):
                if chunk.text:
                    parts.append(chunk.text)
                    yield _sse({"event": "delta", "text": chunk.text})
        except Exception as exc:  # noqa: BLE001 - 错误要送到前端，不能只留在日志里
            error = str(exc)
            logger.error(f"[studio] 生成失败 node={node_id}: {exc}")
            yield _sse({"event": "error", "message": error})
        finally:
            text = "".join(parts)
            elapsed = int((time.perf_counter() - started) * 1000)
            await store.finish_run(
                run_id,
                status="ok" if not error else "failed",
                output_chars=len(text),
                error=error,
                elapsed_ms=elapsed,
            )
            # 只有明确要求保存时才写库；前端先展示、用户点「采用」再落，避免污染原稿
            if body.save and text and not error:
                content = (node.get("content") or "") if body.task_type == "continue" else ""
                merged = (content + ("\n\n" if content else "") + text) if content else text
                # 续写合并进正文也是 AI 写的内容，来源记 ai_draft，不让人误以为是手改的
                await store.update_node(node_id, content=merged, snapshot=True,
                                        snapshot_source="ai_draft", run_id=run_id)
                # 写库了就把这一节重建索引，下次续写能检索到
                fresh = await store.get_node(node_id)
                if fresh:
                    try:
                        await indexer.index_node(store, fresh)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[studio] 索引失败 node={node_id}: {exc}")
                    # 检查不阻塞保存（设计如此），结果留给面板上的告警条
                    try:
                        _work = await store.get_work(fresh["work_id"])
                        if _work:
                            _resolved = content_types.resolve(
                                _work.get("kind") or "article",
                                _work.get("scale") or "", _work.get("subtype") or "")
                            await consistency.run_checks(store, _work, fresh, resolved=_resolved)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[studio] 检查失败 node={node_id}: {exc}")
            yield _sse({
                "event": "done",
                "chars": len(text),
                "elapsed_ms": elapsed,
                "saved": bool(body.save and text and not error),
            })
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== 内容类型配置 ====================

@router.get("/studio/content-types", dependencies=[Depends(verify_key)])
async def get_content_types():
    """前端下拉框的数据源。新增文种只改 content-types.yaml，页面不用动。"""
    return {
        "kinds": content_types.list_kinds(),
        "error": content_types.get_types().get("_load_error", ""),
    }


@router.post("/studio/content-types/reload", dependencies=[Depends(verify_key)])
async def reload_content_types(request: Request):
    """改完 YAML 后调一次：重读配置 + 同步内置模板到库。"""
    data = content_types.reload_types()
    await _store(request).sync_templates()
    return {
        "ok": True,
        "kinds": len(data.get("kinds") or {}),
        "templates": len(data.get("templates") or []),
        "error": data.get("_load_error", ""),
    }


# ==================== 公共小工具 ====================

async def _template_of(store, work: dict) -> Optional[dict]:
    if not work.get("template_id"):
        return None
    for tpl in await store.list_templates():
        if tpl["id"] == work["template_id"]:
            return tpl
    return None


def _work_meta(work: dict) -> dict[str, Any]:
    try:
        meta = json.loads(work.get("meta_json") or "{}")
        return meta if isinstance(meta, dict) else {}
    except json.JSONDecodeError:
        return {}


def _norm_sections(items: Any) -> list[dict[str, Any]]:
    """把模型给的小节列表归一成 {heading, prompt, target_words}。"""
    out: list[dict[str, Any]] = []
    if isinstance(items, dict):
        items = items.get("sections") or items.get("outline") or []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        heading = str(it.get("heading") or it.get("title") or "").strip()
        if not heading:
            continue
        try:
            words = int(it.get("target_words") or 0)
        except (TypeError, ValueError):
            words = 0
        out.append({
            "heading": heading[:80],
            "prompt": str(it.get("prompt") or it.get("brief") or "").strip()[:400],
            "target_words": words or 800,
        })
    return out


# ==================== 大纲 → 逐节 → 拼装 ====================

class OutlineRequest(BaseModel):
    instruction: str = ""
    model: str = ""
    mode: str = "quick"
    sections: int = 0        # 0 = 按模板的小节数
    append: bool = False     # 已有节点时是否允许追加


class PipelineRequest(BaseModel):
    model: str = ""
    mode: str = "quick"
    only_empty: bool = True
    limit: int = 0           # 0 = 全部
    instruction: str = ""
    stop_on_error: bool = True
    pause_ms: int = 600      # 节之间歇一下，避免触发站点限流
    # 每节写完后立即建索引 + 生成摘要：后面的节才能引用前面的内容
    index: bool = True
    summarize: bool = True
    # 小说：每节写完就抽一次设定（否则下一节 S6 还是空的），并跑一致性检查（零成本）
    extract: bool = True
    check: bool = True


def _outline_prompt(work: dict, resolved: dict, template: Optional[dict],
                    sections: int, instruction: str) -> tuple[str, str]:
    layers = resolved.get("layers") or []
    container = resolved.get("container_kind")
    tpl_outline = consistency.template_outline(template)  # 同一套解析，避免两处漂移
    base_n = sections or (len(tpl_outline) or 5)

    lines = [f"作品标题：{work['title']}"]
    if work.get("premise"):
        lines.append(f"主旨：{work['premise']}")
    lines.append(f"类型：{resolved.get('label')} / {resolved.get('subtype_label')}")
    if resolved.get("target_words"):
        lines.append(f"目标总字数：{resolved['target_words']}")
    if resolved.get("note"):
        lines.append(f"注意：{resolved['note']}")
    if template and template.get("outline_json"):
        from app.studio.assembler import _fmt_outline  # 同一份格式化，避免两处漂移
        lines.append("参考骨架（可增删改，但保持同类结构与顺序）：\n" + _fmt_outline(template["outline_json"]))
    if instruction:
        lines.append(f"额外要求：{instruction}")

    if container:
        shape = ('{"parts":[{"title":"第一部分标题","sections":'
                 '[{"heading":"小节标题","prompt":"本节要写什么","target_words":800}]}]}')
        ask = f"请给出 1-3 个{layers[0].get('label', '部分')}，每部分 2-5 个小节（共约 {base_n} 节）"
    else:
        shape = '{"sections":[{"heading":"小节标题","prompt":"本节要写什么","target_words":800}]}'
        ask = f"请给出约 {base_n} 个小节"

    user = "\n".join(lines) + f"\n\n{ask}，严格按下面的 JSON 结构输出，不要解释、不要代码块：\n{shape}"
    system = (
        "你是中文写作的策划编辑。你只输出合法 JSON，不输出任何解释、前言或 Markdown 代码块标记。"
        "小节标题要具体可检索，不要「概述」「其他」这类空标题。"
    )
    return system, user


@router.post("/studio/works/{work_id}/outline", dependencies=[Depends(verify_key)])
async def generate_outline(work_id: str, body: OutlineRequest, request: Request):
    """生成目录并落成节点树。已有节点时默认拒绝，避免把人工目录冲掉。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    resolved = content_types.resolve(work.get("kind") or "article",
                                    work.get("scale") or "", work.get("subtype") or "")
    template = await _template_of(store, work)

    existing = await store.list_readable(work_id)
    if existing and not body.append:
        raise HTTPException(
            status_code=409,
            detail=f"已有 {len(existing)} 个内容节点。确认追加请传 append=true，否则先在左侧手动删除。",
        )

    system, user = _outline_prompt(work, resolved, template, body.sections, body.instruction)
    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    started = time.perf_counter()

    # 模型可能返回不合法 JSON（常见：字符串里未转义换行、输出被截断）。
    # 第一次失败就重试一次并明确要求「只输出 JSON」，代价是多一次调用，
    # 但比让用户自己点重试体验好。
    parsed: Any = None
    last_text = ""
    last_run_id = ""
    repair = "\n\n上一次输出不是合法 JSON。现在只输出一个合法 JSON 对象，不要解释、不要代码块、不要省略号。"
    for attempt in (1, 2):
        prompt = user if attempt == 1 else user + repair
        last_run_id = await store.start_run(
            work_id=work_id, node_id=None, task_type="outline", model=model,
            provider=gateway.resolve(model)[0], prompt_chars=len(system) + len(prompt),
        )
        try:
            res = await gateway.complete(model=model, system=system, prompt=prompt,
                                        mode=body.mode or "quick")
        except Exception as exc:  # noqa: BLE001 - 上游失败要能读明白
            await store.finish_run(last_run_id, status="failed", error=str(exc),
                                   elapsed_ms=int((time.perf_counter() - started) * 1000))
            logger.error(f"[studio] 大纲生成失败 work={work_id} attempt={attempt}: {exc}")
            if attempt == 2:
                raise HTTPException(status_code=502, detail=f"大纲生成失败：{exc}") from exc
            continue

        last_text = res.text
        got = _parse_json_loose(res.text)
        if isinstance(got, list):
            got = {"sections": got}
        if isinstance(got, dict) and (got.get("parts") or got.get("sections") or got.get("outline")):
            parsed = got
            await store.finish_run(last_run_id, status="ok", output_chars=len(res.text),
                                   elapsed_ms=int((time.perf_counter() - started) * 1000))
            break
        await store.finish_run(last_run_id, status="failed", output_chars=len(res.text),
                               error="模型返回不是合法 JSON",
                               elapsed_ms=int((time.perf_counter() - started) * 1000))
        logger.warning(f"[studio] 大纲 JSON 解析失败 attempt={attempt} chars={len(res.text)}")

    if parsed is None:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "模型两次都没返回合法 JSON，可换个模型或减少节数",
                "chars": len(last_text),
                "head": last_text[:200],
                "tail": last_text[-300:],
            },
        )

    container_kind = resolved.get("container_kind")
    leaf_kind = resolved.get("leaf_kind") or "section"
    created: list[dict[str, Any]] = []
    outline_meta: dict[str, Any] = {}

    parts = parsed.get("parts")
    if container_kind and isinstance(parts, list) and parts:
        for p in parts:
            if not isinstance(p, dict):
                continue
            title = str(p.get("title") or "未命名部分").strip()[:80]
            parent = await store.create_node(work_id=work_id, kind=container_kind, title=title)
            created.append(parent)
            for sec in _norm_sections(p.get("sections")):
                n = await store.create_node(
                    work_id=work_id, kind=leaf_kind, title=sec["heading"], parent_id=parent["id"]
                )
                created.append(n)
                outline_meta[n["id"]] = {"prompt": sec["prompt"], "target_words": sec["target_words"]}
    else:
        secs = _norm_sections(parsed.get("sections") or parsed.get("outline") or [])
        if not secs:
            await store.finish_run(last_run_id, status="failed", output_chars=len(last_text),
                                   error="JSON 里没有小节",
                                   elapsed_ms=int((time.perf_counter() - started) * 1000))
            raise HTTPException(status_code=502, detail="模型返回的 JSON 里没有 sections")
        parent_id = None
        if container_kind:
            parent = await store.create_node(work_id=work_id, kind=container_kind, title=work["title"])
            created.append(parent)
            parent_id = parent["id"]
        for sec in secs:
            n = await store.create_node(
                work_id=work_id, kind=leaf_kind, title=sec["heading"], parent_id=parent_id
            )
            created.append(n)
            outline_meta[n["id"]] = {"prompt": sec["prompt"], "target_words": sec["target_words"]}

    # 把每节的要点落到 works.meta_json：拼装流水线时要用，不能只活在内存里
    meta = _work_meta(work)
    meta.setdefault("outline", {}).update(outline_meta)
    await store.update_work(work_id, meta_json=json.dumps(meta, ensure_ascii=False))

    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "run_id": last_run_id,
        "nodes": [{"id": n["id"], "title": n["title"], "kind": n["kind"],
                   "ordinal": n.get("ordinal")} for n in created],
        "sections": len(outline_meta),
        "elapsed_ms": elapsed,
        "raw": last_text[:600],
    }


@router.post("/studio/works/{work_id}/pipeline", dependencies=[Depends(verify_key)])
async def run_pipeline(work_id: str, body: PipelineRequest, request: Request):
    """逐节生成：每节独立装配、独立落库，中途某个失败不影响已完成的部分。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    resolved = content_types.resolve(work.get("kind") or "article",
                                    work.get("scale") or "", work.get("subtype") or "")
    template = await _template_of(store, work)
    work_meta = _work_meta(work)

    nodes = await store.list_readable(work_id, only_empty=body.only_empty)
    if body.limit > 0:
        nodes = nodes[: body.limit]
    if not nodes:
        raise HTTPException(
            status_code=400,
            detail="没有待生成的节：先点「生成大纲」，或把 only_empty 设为 false 重跑已有内容",
        )

    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)

    # 开跑前把全书旧内容补齐索引（增量，没变就跳过）。
    # 手改过的正文不会自动入库，不补这一步，第 20 节就检索不到第 3 节的新版本。
    pre_index: dict[str, Any] = {}
    if body.index:
        try:
            pre_index = await indexer.reindex_work(store, work_id, include_sources=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[studio] 流水线前索引失败 work={work_id}: {exc}")

    async def event_stream():
        started = time.perf_counter()
        generated = 0
        total_chars = 0
        failed: list[str] = []
        yield _sse({
            "event": "plan", "total": len(nodes), "model": model,
            "pre_indexed": pre_index.get("chunks_total", 0),
            "nodes": [{"id": n["id"], "title": n["title"], "ordinal": n.get("ordinal")} for n in nodes],
        })
        for idx, item in enumerate(nodes, 1):
            node = await store.get_node(item["id"])
            if not node:
                continue
            instruction = _node_instruction(node, work_meta, body.instruction)
            try:
                result = await assemble(
                    store, work, node, "article_draft", instruction,
                    template=template, resolved=resolved,
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(item["id"])
                yield _sse({"event": "node_error", "node_id": item["id"], "message": f"装配失败：{exc}"})
                if body.stop_on_error:
                    break
                continue

            run_id = await store.start_run(
                work_id=work_id, node_id=item["id"], task_type="article_draft",
                model=model, provider=gateway.resolve(model)[0], prompt_chars=result.total_chars,
            )
            yield _sse({
                "event": "node_start", "node_id": item["id"], "title": node["title"],
                "index": idx, "total": len(nodes),
            })
            parts: list[str] = []
            error = ""
            t0 = time.perf_counter()
            try:
                async for chunk in gateway.stream(
                    model=model, system=result.system, prompt=result.user, mode=body.mode or "quick"
                ):
                    if chunk.text:
                        parts.append(chunk.text)
                        yield _sse({"event": "delta", "node_id": item["id"], "text": chunk.text})
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                logger.error(f"[studio] 流水线生成失败 node={item['id']}: {exc}")
                yield _sse({"event": "error", "node_id": item["id"], "message": error})

            text = "".join(parts)
            elapsed = int((time.perf_counter() - t0) * 1000)
            await store.finish_run(
                run_id, status="ok" if not error else "failed",
                output_chars=len(text), error=error, elapsed_ms=elapsed,
            )
            if text and not error:
                await store.update_node(item["id"], content=text, snapshot=True,
                                        snapshot_source="ai_draft", run_id=run_id)
                generated += 1
                total_chars += len(text)
                yield _sse({"event": "node_done", "node_id": item["id"],
                            "chars": len(text), "elapsed_ms": elapsed})
                # 边写边建索引与摘要：不这样做，后面的节就“记不住”前面写了什么
                fresh = await store.get_node(item["id"])
                if body.index and fresh:
                    try:
                        n = await indexer.index_node(store, fresh)
                        yield _sse({"event": "node_indexed", "node_id": item["id"], "chunks": n})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[studio] 索引失败 node={item['id']}: {exc}")
                if body.summarize and fresh:
                    try:
                        sm = await summarizer.summarize_node(gateway, store, fresh, model=model)
                        if sm:
                            yield _sse({"event": "node_summary", "node_id": item["id"],
                                        "chars": len(sm)})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[studio] 摘要失败 node={item['id']}: {exc}")
                # 设定抽取：下一节的 S6 就靠这一步。放在摘要之后、检查之前，
                # 因为检查要读刚抽出来的实体状态（谁死了、东西归谁）。
                if body.extract and (work.get("kind") or "") == "novel" and fresh:
                    try:
                        ex = await graph.extract_node(gateway, store, work, fresh, model=model)
                        if not ex.get("error"):
                            yield _sse({"event": "node_extracted", "node_id": item["id"],
                                        "entities": ex.get("entities", 0),
                                        "relations": ex.get("relations", 0),
                                        "foreshadows": ex.get("foreshadows", 0)})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[studio] 抽取失败 node={item['id']}: {exc}")
                if body.check and fresh:
                    try:
                        found = await consistency.run_checks(store, work, fresh, resolved=resolved,
                                                             template=template)
                        if found:
                            yield _sse({"event": "node_issues", "node_id": item["id"],
                                        "count": len(found),
                                        "rules": sorted({i["rule"] for i in found})})
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[studio] 检查失败 node={item['id']}: {exc}")
            else:
                failed.append(item["id"])
                if body.stop_on_error:
                    break
            if body.pause_ms > 0 and idx < len(nodes):
                await asyncio.sleep(body.pause_ms / 1000)

        yield _sse({
            "event": "done", "generated": generated, "total": len(nodes),
            "chars": total_chars, "failed": failed,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _render_markdown(store, work: dict, include_empty: bool) -> tuple[str, int]:
    tree = await store.list_tree(work["id"])
    by_id = {n["id"]: n for n in tree}
    readable = [n for n in tree if n.get("ordinal") is not None]
    readable.sort(key=lambda n: n.get("ordinal") or 0)

    blocks: list[str] = []
    last_container = None
    used = 0
    for item in readable:
        node = await store.get_node(item["id"])
        content = ((node or {}).get("content") or "").strip()
        if not content and not include_empty:
            continue
        parent = by_id.get(item.get("parent_id"))
        if parent and parent["id"] != last_container:
            blocks.append(f"## {parent['title']}")
            last_container = parent["id"]
        blocks.append(f"### {item['title']}")
        if content:
            blocks.append(content)
        used += 1

    if not blocks:
        return "", 0
    markdown = f"# {work['title']}\n\n" + "\n\n".join(blocks) + "\n"
    return markdown, used


@router.post("/studio/works/{work_id}/assemble", dependencies=[Depends(verify_key)])
async def assemble_document(work_id: str, request: Request, include_empty: bool = False):
    """把各节按全序拼成一份 Markdown。不调用模型，纯拼装。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    markdown, used = await _render_markdown(store, work, include_empty)
    return {"markdown": markdown, "chars": len(markdown), "nodes": used}


@router.get("/studio/works/{work_id}/export", dependencies=[Depends(verify_key)])
async def export_document(work_id: str, request: Request, include_empty: bool = False):
    """直接下载 Markdown 文件。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    markdown, used = await _render_markdown(store, work, include_empty)
    if not markdown:
        raise HTTPException(status_code=400, detail="还没有可导出的内容")
    safe = re.sub(r'[\\/:*?"<>|]+', "_", work["title"])[:60] or "draft"
    # 头必须是 latin-1，中文文件名要 URL 编码，否则 Starlette 会直接报 UnicodeEncodeError
    encoded = quote(f"{safe}.md")
    return PlainTextResponse(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=export.md; filename*=UTF-8''{encoded}",
            "X-Nodes-Used": str(used),
        },
    )


# ==================== 加工类任务（多候选） ====================

_TRANSFORM_ACTIONS: dict[str, tuple[str, str]] = {
    "rewrite": ("重写", "在不改变事实与结论的前提下重写这段文字，改善节奏、句长与衔接；不得新增事实。"),
    "condense": ("压缩", "把这段压缩到原文的 40%-60%，保留全部关键事实、数字与专有名词。"),
    "expand": ("扩写", "把这段扩写到原文的 1.8-2.2 倍，补充细节、例子与过渡；不得虚构事实或数据。"),
    "polish": ("润色", "只做语言层面的修改：错别字、病句、重复、标点；不改事实、结构与顺序。"),
    "translate": ("翻译", "把这段译成目标语言，术语保持一致，保留原文的段落划分。"),
    "split": ("拆分", "把这段按语义拆成 2-4 个带小标题的段落，内容不增不减。"),
    "merge": ("合并", "把这段合并成一段连贯的话，去掉重复表述，不丢信息。"),
}


class TransformRequest(BaseModel):
    text: str = Field(min_length=1, max_length=60000)
    action: str = "rewrite"
    instruction: str = ""
    lang: str = "英文"
    candidates: int = 1
    model: str = ""
    mode: str = "quick"
    work_id: str = ""


@router.post("/studio/transform", dependencies=[Depends(verify_key)])
async def transform(body: TransformRequest, request: Request):
    """加工类任务：一段文字进，N 个候选出。

    没有做成流式：改写一段几百字通常 5-15 秒，而多候选需要横向比较，
    边生成边跳动反而难判断。每个候选都记一条 runs，variant 递增，
    这样后面做盲评/自动打分不需要改表。
    """
    if body.action not in _TRANSFORM_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的动作: {body.action}")
    label, brief = _TRANSFORM_ACTIONS[body.action]
    if body.action == "translate":
        brief = brief.replace("目标语言", body.lang or "英文")

    store = _store(request)
    style = ""
    work = None
    if body.work_id:
        work = await store.get_work(body.work_id)
        if work:
            resolved = content_types.resolve(work.get("kind") or "article",
                                            work.get("scale") or "", work.get("subtype") or "")
            style = resolved.get("style_prompt") or ""

    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    system = "你是中文编辑，只输出加工后的正文，不解释、不加前后缀。"
    if style:
        system += "\n文体要求：" + style
    user_parts = [f"<task>{brief}</task>"]
    if body.instruction:
        user_parts.append(f"<extra>{body.instruction}</extra>")
    user_parts.append(f"<text>\n{body.text}\n</text>")
    user = "\n\n".join(user_parts)

    n = max(1, min(int(body.candidates or 1), 3))
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for i in range(n):
        run_id = await store.start_run(
            work_id=body.work_id or "transform",
            node_id=None,
            task_type=f"transform:{body.action}",
            model=model,
            provider=gateway.resolve(model)[0],
            prompt_chars=len(system) + len(user),
            variant=i,
        )
        started = time.perf_counter()
        try:
            res = await gateway.complete(model=model, system=system, prompt=user, mode=body.mode or "quick")
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - started) * 1000)
            await store.finish_run(run_id, status="failed", error=str(exc), elapsed_ms=elapsed)
            errors.append(f"候选 {i + 1} 失败：{exc}")
            continue
        elapsed = int((time.perf_counter() - started) * 1000)
        await store.finish_run(run_id, status="ok", output_chars=len(res.text), elapsed_ms=elapsed)
        results.append({
            "variant": i,
            "run_id": run_id,
            "text": res.text.strip(),
            "chars": len(res.text.strip()),
            "elapsed_ms": elapsed,
        })

    if not results:
        raise HTTPException(status_code=502, detail={"message": "全部候选都失败了", "errors": errors})
    return {
        "action": body.action, "label": label, "model": model,
        "candidates": results, "errors": errors,
    }


# ==================== 索引 / 检索（M2） ====================

class IndexRequest(BaseModel):
    force: bool = False
    include_sources: bool = True


@router.post("/studio/works/{work_id}/index", dependencies=[Depends(verify_key)])
async def build_index(work_id: str, body: IndexRequest, request: Request):
    """建/重建索引。默认增量：只重建正文变过的节点与新增素材。"""
    await _require_work(request, work_id)
    return await indexer.reindex_work(
        _store(request), work_id, force=body.force, include_sources=body.include_sources
    )


@router.get("/studio/works/{work_id}/index/status", dependencies=[Depends(verify_key)])
async def get_index_status(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await indexer.index_status(_store(request), work_id)


class SearchRequest(BaseModel):
    q: str = Field(min_length=1, max_length=500)
    top_k: int = 8
    scope: Literal["body", "source", "both"] = "both"


@router.post("/studio/works/{work_id}/search", dependencies=[Depends(verify_key)])
async def search_work(work_id: str, body: SearchRequest, request: Request):
    """检索调试：直接看 FTS5 召回了什么、排序分多少。"""
    store = _store(request)
    await _require_work(request, work_id)
    kinds = ("body", "source") if body.scope == "both" else (body.scope,)
    hits = await retrieval.retrieve(
        store,
        work_id,
        query=body.q,
        kinds=kinds,
        top_k=max(1, min(body.top_k, 30)),
        budget_chars=20000,
    )
    return {"query": body.q, "kinds": list(kinds), "hits": hits, "count": len(hits)}


class SummarizeRequest(BaseModel):
    scope: Literal["nodes", "containers", "work", "all"] = "nodes"
    force: bool = False
    model: str = ""


@router.post("/studio/nodes/{node_id}/summarize", dependencies=[Depends(verify_key)])
async def summarize_node_api(node_id: str, request: Request, force: bool = False):
    store = _store(request)
    node = await store.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    model = pick_model(request.app.state.registry, None)
    text = await summarizer.summarize_node(
        _gateway(request), store, node, model=model, force=force
    )
    if not text:
        raise HTTPException(status_code=400, detail="正文太短（<200 字）或生成失败")
    return {"node_id": node_id, "summary": text, "chars": len(text)}


@router.post("/studio/works/{work_id}/summarize", dependencies=[Depends(verify_key)])
async def summarize_work_api(work_id: str, body: SummarizeRequest, request: Request):
    """批量摘要：nodes → containers → work，逐层合并。"""
    store = _store(request)
    await _require_work(request, work_id)
    model = pick_model(request.app.state.registry, body.model or None)
    done = await summarizer.summarize_work_batch(
        _gateway(request), store, work_id, model=model, scope=body.scope, force=body.force
    )
    return {"ok": True, "model": model, **done}


# ==================== 图谱与一致性（M3） ====================

class GraphExtractRequest(BaseModel):
    node_id: str = ""
    all: bool = False        # 把整本待抽的节都抽（受 limit 限制，可反复调）
    force: bool = False      # 内容没变也重抽
    limit: int = 5           # 单次最多抽几节，避免一个请求跑几分钟
    model: str = ""


class CardCreate(BaseModel):
    id: str = ""
    kind: str = "character"
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    summary: str = ""
    # 例：{"status": "dead", "rank": "筑基层"}；有 status 会同步到实体，参与一致性检查
    data_json: str = "{}"
    pinned: bool = True


class CheckRequest(BaseModel):
    node_id: str = ""
    all: bool = True


@router.get("/studio/works/{work_id}/graph", dependencies=[Depends(verify_key)])
async def get_graph(work_id: str, request: Request, refresh: bool = False):
    """面板数据：统计 + 实体 + 关系 + 伏笔 + 设定卡。

    refresh=true 清掉内存图缓存：手工改过库、或多进程并发时用得上。
    """
    store = _store(request)
    await _require_work(request, work_id)
    if refresh:
        graph.invalidate(work_id)
    return {
        "stats": await store.graph_stats(work_id),
        "entities": await store.list_entities(work_id),
        "relations": await store.list_relations(work_id),
        "foreshadows": await store.list_foreshadows(work_id),
        "cards": await store.list_cards(work_id),
    }


@router.get("/studio/works/{work_id}/graph/subgraph", dependencies=[Depends(verify_key)])
async def get_subgraph(work_id: str, request: Request, text: str = "", hops: int = 2):
    """看一眼本轮 S6 会装什么——调 prompt 时最常用的排查入口。"""
    await _require_work(request, work_id)
    sg = await graph.subgraph(_store(request), work_id, text=text,
                              hops=max(1, min(hops, 3)), limit=40)
    return {**sg, "preview": graph.format_subgraph(sg, budget=2500)}


@router.post("/studio/works/{work_id}/graph/extract", dependencies=[Depends(verify_key)])
async def extract_graph(work_id: str, body: GraphExtractRequest, request: Request):
    """抽取设定并合入图谱。

    同步返回而不是 SSE：单节约 5 秒，前端转个圈就行。整本用 all=true
    分批推进（默认每批 5 节），反复调用直到 remaining 为 0，
    这样任何一次超时都不会把已抽好的成果丢掉。
    """
    store = _store(request)
    work = await _require_work(request, work_id)
    if (work.get("kind") or "") != "novel":
        raise HTTPException(status_code=400, detail="只有小说建图谱；单篇文档用事实卡与术语表")
    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)

    if body.node_id:
        node = await store.get_node(body.node_id)
        if not node or node.get("work_id") != work_id:
            raise HTTPException(status_code=404, detail="节点不存在")
        targets = [node]
    elif body.all:
        targets = []
        for item in await store.list_readable(work_id):
            node = await store.get_node(item["id"])
            if node and (node.get("content") or "").strip():
                targets.append(node)
    else:
        raise HTTPException(status_code=400, detail="传 node_id，或者 all=true")

    limit = max(1, min(body.limit or 5, 30))
    done = skipped = failed = 0
    totals = {"entities": 0, "relations": 0, "events": 0, "states": 0, "foreshadows": 0}
    errors: list[str] = []
    tries = 0
    for node in targets:
        if done + failed >= limit or tries >= limit * 3:
            break
        tries += 1
        res = await graph.extract_node(gateway, store, work, node, model=model, force=body.force)
        if res.get("error"):
            failed += 1
            errors.append(f"{node['title']}: {res['error']}")
            continue
        if res.get("skipped"):
            skipped += 1
            continue
        done += 1
        for key in totals:
            totals[key] += int(res.get(key) or 0)

    return {
        "ok": failed == 0,
        "model": model,
        "total": len(targets),
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "remaining": max(0, len(targets) - done - skipped - failed),
        "errors": errors[:5],
        "stats": await store.graph_stats(work_id),
        **totals,
    }


@router.delete("/studio/works/{work_id}/graph", dependencies=[Depends(verify_key)])
async def clear_graph(work_id: str, request: Request):
    """抽乱了重来。设定卡不删——那是作者手写的。"""
    store = _store(request)
    await _require_work(request, work_id)
    cleared = await store.clear_graph(work_id)
    graph.invalidate(work_id)
    return {"ok": True, **cleared}


@router.get("/studio/works/{work_id}/cards", dependencies=[Depends(verify_key)])
async def list_cards(work_id: str, request: Request, kind: str = ""):
    await _require_work(request, work_id)
    return await _store(request).list_cards(work_id, kind=kind)


@router.post("/studio/works/{work_id}/cards", dependencies=[Depends(verify_key)])
async def upsert_card(work_id: str, body: CardCreate, request: Request):
    """作者手写的设定卡：抽取时是权威提示，面板上是「圣经」。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    if (work.get("kind") or "") != "novel":
        raise HTTPException(status_code=400, detail="设定卡只用于小说")
    try:
        data = json.loads(body.data_json or "{}")
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="data_json 不是合法 JSON") from exc

    card = await store.upsert_card(
        work_id, kind=body.kind, name=body.name, aliases=body.aliases,
        summary=body.summary, data_json=json.dumps(data, ensure_ascii=False),
        pinned=body.pinned, card_id=body.id,
    )
    # 卡片就是作者的「圣经」，每张都该在图上有节点；否则「作者写了这个设定」
    # 对下游（S6、一致性检查）完全不可见。status 有值时才额外写状态。
    status = str(data.get("status") or "").strip()
    if body.kind in graph.ENTITY_KINDS:
        entity = await store.upsert_entity(
            work_id, kind=body.kind, name=body.name, canonical=graph.canon(body.name),
            aliases=body.aliases, first_ordinal=0,
        )
        await store.link_entity_card(entity["id"], card["id"])
        if status in graph.STATUSES:
            # data_json 可带 status_ordinal：从第几节起是这个状态。
            # 卡片说「第三章死」，那前面几章出场就不能报复活。
            try:
                status_ordinal = int(data.get("status_ordinal") or 0)
            except (TypeError, ValueError):
                status_ordinal = 0
            await store.set_entity_status(entity["id"], status, status_ordinal)
    graph.invalidate(work_id)
    return card


@router.delete("/studio/cards/{card_id}", dependencies=[Depends(verify_key)])
async def delete_card(card_id: str, request: Request):
    ok = await _store(request).delete_card(card_id)
    if not ok:
        raise HTTPException(status_code=404, detail="设定卡不存在")
    return {"ok": True}


@router.post("/studio/works/{work_id}/check", dependencies=[Depends(verify_key)])
async def run_check(work_id: str, body: CheckRequest, request: Request):
    """跑一致性检查。全部是本地结构化比对，不调模型。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    resolved = content_types.resolve(
        work.get("kind") or "article", work.get("scale") or "", work.get("subtype") or "")
    template = await _template_of(store, work)

    created: list[dict[str, Any]] = []
    if body.node_id:
        node = await store.get_node(body.node_id)
        if not node or node.get("work_id") != work_id:
            raise HTTPException(status_code=404, detail="节点不存在")
        created += await consistency.run_checks(store, work, node, resolved=resolved,
                                                template=template)
    else:
        for item in await store.list_readable(work_id):
            node = await store.get_node(item["id"])
            if not node:
                continue
            created += await consistency.run_checks(store, work, node, resolved=resolved,
                                                    template=template)
        created += await consistency.run_checks(store, work, None, resolved=resolved,
                                                template=template)
    return {
        "created": len(created),
        "issues": created,
        "summary": await consistency.check_summary(store, work_id),
    }


@router.post("/studio/issues/{issue_id}/resolve", dependencies=[Depends(verify_key)])
async def resolve_issue(issue_id: str, request: Request):
    ok = await _store(request).resolve_issue(issue_id)
    if not ok:
        raise HTTPException(status_code=404, detail="告警不存在")
    return {"ok": True}


@router.get("/studio/works/{work_id}/issues/summary", dependencies=[Depends(verify_key)])
async def issues_summary(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await consistency.check_summary(_store(request), work_id)


# ==================== 任务队列（M4） ====================

class BatchRequest(BaseModel):
    task_type: Literal["draft", "beats", "polish", "check", "summarize", "extract"] = "draft"
    node_ids: list[str] = Field(default_factory=list)  # 空 = 按 only_empty 选
    only_empty: bool = True
    limit: int = 0            # 0 = 不限
    model: str = ""
    mode: str = "quick"
    instruction: str = ""
    priority: int = 5         # 人工触发给 9
    auto_beats: bool = True   # 小说：每章先出节拍卡再写正文
    triage_every: int = 0     # 0 = 用默认 10；调试/验收可用小值
    index: bool = True
    summarize: bool = True
    extract: bool = True
    check: bool = True


class TriageRequest(BaseModel):
    verdict: Literal["ok", "rewrite"] = "ok"


@router.post("/studio/works/{work_id}/batch", dependencies=[Depends(verify_key)])
async def enqueue_batch(work_id: str, body: BatchRequest, request: Request):
    """批量入队：把一节节的写作任务排进队列，worker 串行跑。

    与 `/pipeline` 的区别：pipeline 是一个 SSE 连接里从头跑到尾，
    关了页面就断；batch 落库成 job，断网、重启、第二天接着跑都行。
    """
    store = _store(request)
    work = await _require_work(request, work_id)
    queue = getattr(request.app.state, "studio_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="任务队列未启动")

    is_novel = (work.get("kind") or "article") == "novel"
    if body.task_type in ("extract", "beats") and not is_novel:
        raise HTTPException(status_code=400, detail="该任务只用于小说")

    # 选节点：指定优先，否则按「有没有正文」筛
    if body.node_ids:
        nodes = []
        for nid in body.node_ids:
            node = await store.get_node(nid)
            if node and node.get("work_id") == work_id:
                nodes.append(node)
    else:
        nodes = []
        for item in await store.list_readable(work_id):
            node = await store.get_node(item["id"])
            if not node:
                continue
            has_text = bool((node.get("content") or "").strip())
            if body.only_empty and has_text:
                continue
            if body.task_type in ("polish", "check", "extract", "summarize") and not has_text:
                continue
            nodes.append(node)
    nodes.sort(key=lambda n: int(n.get("ordinal") or 0))
    if body.limit and body.limit > 0:
        nodes = nodes[: body.limit]
    if not nodes:
        raise HTTPException(status_code=400, detail="没有可入队的节点（检查 only_empty 与正文是否为空）")

    payload = json.dumps({
        "model": body.model, "mode": body.mode, "instruction": body.instruction,
        "auto_beats": body.auto_beats, "index": body.index, "summarize": body.summarize,
        "extract": body.extract, "check": body.check,
        "triage_every": body.triage_every,
    }, ensure_ascii=False)

    queued: list[dict[str, Any]] = []
    deduped = 0
    for node in nodes:
        # 幂等键带上任务类型：同一节点「先写后查」是两个合法任务
        idem = f"{work_id}:{node['id']}:{body.task_type}"
        job = await store.enqueue_job(
            work_id=work_id, node_id=node["id"], task_type=body.task_type,
            payload_json=payload, priority=max(1, min(body.priority, 9)), idem_key=idem,
        )
        if job.get("deduped"):
            deduped += 1
            continue
        queued.append({"job_id": job["id"], "node_id": node["id"], "title": node["title"]})

    await queue.start()
    reason = ""
    if body.task_type == "draft" and is_novel and body.auto_beats:
        missing = [n["title"] for n in nodes if not (n.get("summary") or "").strip()]
        if missing:
            reason = f"{len(missing)} 章缺节拍卡，会在写作前自动补"
    return {
        "queued": len(queued),
        "deduped": deduped,
        "nodes": queued,
        "note": reason,
        "queue": await queue.status(work_id),
    }


@router.get("/studio/queue/status", dependencies=[Depends(verify_key)])
async def queue_status(request: Request, work_id: str = ""):
    queue = getattr(request.app.state, "studio_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="任务队列未启动")
    return await queue.status(work_id)


@router.post("/studio/queue/pause", dependencies=[Depends(verify_key)])
async def queue_pause(request: Request, reason: str = ""):
    queue = getattr(request.app.state, "studio_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="任务队列未启动")
    await queue.pause(reason or "手动暂停")
    return await queue.status()


@router.post("/studio/queue/resume", dependencies=[Depends(verify_key)])
async def queue_resume(request: Request):
    queue = getattr(request.app.state, "studio_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="任务队列未启动")
    await queue.resume()
    await queue.start()
    return await queue.status()


@router.post("/studio/queue/recover", dependencies=[Depends(verify_key)])
async def queue_recover(request: Request):
    """手动把卡在 running 的任务退回队列：

    正常情况进程启动时会自动做一次，但如果服务是被强行杀掉又马上重启的，
    或者多个进程共用一个库，就需要一个人工入口。
    """
    queue = getattr(request.app.state, "studio_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="任务队列未启动")
    n = await _store(request).reset_running_jobs()
    return {"recovered": n, "queue": await queue.status()}


@router.post("/studio/queue/triage", dependencies=[Depends(verify_key)])
async def queue_triage(body: TriageRequest, request: Request):
    """抽检判定：连续两次「重写」会整队停下。"""
    queue = getattr(request.app.state, "studio_queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="任务队列未启动")
    return await queue.triage(body.verdict)


@router.get("/studio/jobs", dependencies=[Depends(verify_key)])
async def list_jobs(request: Request, work_id: str = "", status: str = "", limit: int = 50):
    rows = await _store(request).list_jobs(work_id=work_id, status=status, limit=limit)
    # 带上标题，前端不用再拉一次树
    store = _store(request)
    for row in rows:
        if row.get("node_id"):
            node = await store.get_node(row["node_id"])
            row["title"] = (node or {}).get("title") or ""
    return rows


@router.post("/studio/jobs/{job_id}/retry", dependencies=[Depends(verify_key)])
async def retry_job(job_id: str, request: Request):
    """把失败的任务重新排队，并把重试次数清零——人工介入过的任务值得再给两次机会。"""
    store = _store(request)
    if not await store.retry_job(job_id):
        raise HTTPException(status_code=404, detail="没有这个失败任务")
    queue = getattr(request.app.state, "studio_queue", None)
    if queue is not None:
        await queue.start()
    return {"ok": True}


@router.post("/studio/jobs/{job_id}/cancel", dependencies=[Depends(verify_key)])
async def cancel_job(job_id: str, request: Request):
    ok = await _store(request).cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="只能取消排队中或失败的任务")
    return {"ok": True}


@router.get("/studio/works/{work_id}/scores", dependencies=[Depends(verify_key)])
async def list_scores(work_id: str, request: Request, limit: int = 60):
    """最近几次生成的得分，按节点汇总（取最新一条）。"""
    await _require_work(request, work_id)
    runs = await _store(request).list_runs(work_id, limit=limit)
    out: dict[str, dict[str, Any]] = {}
    for r in runs:
        nid = r.get("node_id") or ""
        if not nid or nid in out:
            continue
        out[nid] = {"node_id": nid, "score": r.get("score"), "task_type": r["task_type"],
                    "at": r["started_at"], "chars": r.get("output_chars")}
    return list(out.values())

