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
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from app.core.auth import verify_key
from app.llm.gateway import Gateway, pick_model
from app.studio import (
    content_types,
    consistency,
    cost,
    exporters,
    graph,
    indexer,
    prompting,
    publish,
    replace,
    retrieval,
    summarizer,
    tasks,
    tokenize,
    voice,
    wechat,
)
from app.studio.assembler import assemble, record_used_sources
from app.studio.docx_import import docx_to_text
from app.studio.jsonutil import as_list as _as_list
from app.studio.jsonutil import parse_json_loose as _parse_json_loose
from app.studio.prompting import node_instruction as _node_instruction
from app.studio.store import BACKUP_SNAPSHOT_KEEP, BackupError, BUNDLE_FORMAT, LIBRARY_FORMAT

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


async def _final_gate(store, node: dict[str, Any]) -> dict[str, Any]:
    """定稿前要过的那道门：这一节还没处理的硬告警。

    只认 `error`：warn/info 是「八成有问题」，不该拦定稿；只看本节，
    全书级告警（`node_id` 为空，比如伏笔超期）不挡单独一节的定稿——
    那些在发布体检里看。列表只带前 10 条，够人判断就行。
    """
    rows = await store.list_issues(
        node["work_id"], node_id=node["id"], only_open=True, limit=200
    )
    errors = [r for r in rows if (r.get("severity") or "") == "error"]
    return {
        "open_errors": len(errors),
        "issues": [
            {"id": r["id"], "rule": r["rule"], "message": r["message"]}
            for r in errors[:10]
        ],
        "truncated": max(0, len(errors) - 10),
    }


# ==================== 请求模型 ====================

class WorkCreate(BaseModel):
    kind: Literal["novel", "article"] = "article"
    scale: Literal["single", "series"] = "single"
    subtype: str = ""
    # 渠道与 subtype 正交：历史专题既可以发公众号也可以发知乎。
    # 留空就是「不发到任何平台」，只看文种约束。
    channel: str = ""
    title: str = Field(min_length=1, max_length=120)
    premise: str = ""
    # 三态：null = 没指定，按 kind+subtype+channel 自动挑一套；
    # "" = 明确说「不套模板」；具体 id = 用户在下拉框里挑的
    template_id: Optional[str] = None
    target_words: int = 0


class WorkUpdate(BaseModel):
    title: Optional[str] = None
    premise: Optional[str] = None
    # 公众号摘要（≤120 字）：发布页要单独填，不填会被截正文前 54 字
    digest: Optional[str] = None
    # 书架只认这三态：归档是从书架收起，不删任何东西
    status: Optional[Literal["active", "paused", "archived"]] = None
    scale: Optional[str] = None
    subtype: Optional[str] = None
    # 换渠道只影响下一节：已写节点的要点存在 meta_json.outline 里，不动
    channel: Optional[str] = None
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
    # 本节计划：prompt / target_words / pov / time / place。
    # 传对象而不是 JSON 字符串，前端少一次 stringify，后端统一落成 JSON 文本。
    plan_json: Optional[dict[str, Any]] = None
    # 前端点「保存」时传 true，才把当前正文留一份版本；自动保存不传
    snapshot: bool = False
    # 只有「采用 AI 结果」那一条会显式传；手改不传，由路由落成 human
    snapshot_source: Optional[str] = None
    # 采用归因：前端把生成 SSE meta 里的 run_id 带回来，版本行认这根线，
    # 反馈统计才知道哪次生成真的进了正文
    run_id: Optional[str] = None
    # 乐观锁：前端手里那一份的 updated_at。改正文的写入若比库里旧，
    # 说明取到存之间有别处动过（双开/异地/跑批），409 拦下而不是悄悄盖掉
    base_updated_at: Optional[float] = None
    # 看过冲突提示仍要「就存我这份」：只放行上面那道闸，与定稿闸的 force 各管各的
    ignore_conflict: bool = False
    # 定稿闸门：这一节有未处理的硬告警时，要人看过并明确「就这样定」才放行
    force: bool = False


class NodeMove(BaseModel):
    # 同层上/下移：只与紧邻的前/后兄弟互换，不支持跨父搬动
    direction: Literal["up", "down"]


class NodeReparent(BaseModel):
    # 拖到哪个容器末尾；null 表示搬到顶层
    parent_id: Optional[str] = None
    # 非空时插到该兄弟节点之前（优先于 parent_id，落点由它的 parent 决定）
    before_id: str = ""


class NodeSplit(BaseModel):
    # 正文字符下标，升序；第 i 段留在原节，其余各段成为紧跟其后的兄弟节
    offsets: list[int] = Field(default_factory=list, max_length=20)


class NodeMerge(BaseModel):
    # 从这一节起往后吃几节（含自己），至少 2
    count: int = Field(2, ge=2, le=50)
    # 段落间隔；空白串退回空行，免得拼出一坨
    separator: str = "\n\n"


class SourceCreate(BaseModel):
    title: str = ""
    url: str = ""
    kind: str = "note"
    content: str = Field(min_length=1)
    published_at: str = ""


class FactsExtractRequest(BaseModel):
    limit: int = 5
    model: str = ""


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


class SourceUpdate(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[str] = None
    content: Optional[str] = None


class FactUpdate(BaseModel):
    statement: Optional[str] = None
    confidence: Optional[float] = None
    valid_until: Optional[str] = None


class TermUpdate(BaseModel):
    term: Optional[str] = None
    definition: Optional[str] = None
    preferred: Optional[str] = None
    forbidden: Optional[list[str]] = None


class GenerateRequest(BaseModel):
    task_type: str = "article_draft"
    instruction: str = ""
    model: str = ""
    mode: str = "quick"
    # 生成结果是否直接写入节点正文（续写=追加，草稿/润色=替换）
    save: bool = False
    # 多候选时标记这是第几个（0 起），写入 runs.variant 方便事后比较
    variant: int = 0


class BeatsRequest(BaseModel):
    # 用当前还没保存的计划要点重抽也行：传了就以它为准
    focus: str = ""
    model: str = ""


# ==================== 模板 ====================

# 模板导出文件上的格式标记：导入时靠它认出「这是一份模板」，
# 而不是靠有没有 name 字段——作品包里也有 name。
TEMPLATE_FORMAT = "novel-studio.template/1"

# 内置模板只读的统一说辞：前端会把 `detail` 原样显示在按钮旁边，
# 只说「409」用户不知道该怎么做。
_BUILTIN_READONLY = "内置模板只读，请先复制一份再改"


class TemplateCreate(BaseModel):
    kind: Literal["novel", "article"] = "article"
    name: str = Field(min_length=1, max_length=80)
    subtype: str = ""
    channel: str = ""
    outline: list[dict[str, Any]] = Field(default_factory=list)
    style_prompt: str = ""
    checks: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    note: str = ""


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    subtype: Optional[str] = None
    channel: Optional[str] = None
    outline: Optional[list[dict[str, Any]]] = None
    style_prompt: Optional[str] = None
    checks: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    note: Optional[str] = None


class TemplateFromWork(BaseModel):
    # 不填就用「源作品标题 · 骨架」
    name: str = ""
    note: str = ""
    tags: list[str] = Field(default_factory=list)


class TemplatePin(BaseModel):
    # 不传即取反：前端那颗星只要一个点击，没必要先读一遍当前值
    pinned: Optional[bool] = None


def _json_list(raw: Any) -> list:
    """库里存的是 JSON 文本，取出来给前端要还原成数组。"""
    if isinstance(raw, list):
        return raw
    try:
        got = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return got if isinstance(got, list) else []


def _norm_outline(items: Any) -> list[dict[str, Any]]:
    """模板骨架归一成 {heading, prompt, target_words}。

    与 `_norm_sections` 分开：那个收的是模型输出（字段名要放宽），
    这个收的是前端表单（字段已定），但都得挡住「heading 为空」与「字数不是数」——
    空标题的节进了 S0，模型会照着写一段没有主题的话。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        heading = str(it.get("heading") or "").strip()
        if not heading:
            continue
        try:
            words = int(it.get("target_words") or 0)
        except (TypeError, ValueError):
            words = 0
        out.append({
            "heading": heading[:80],
            "prompt": str(it.get("prompt") or "").strip()[:400],
            "target_words": words,
        })
    return out


def _template_payload(tpl: dict[str, Any]) -> dict[str, Any]:
    """导出用的模板快照：去掉 id / 内置标记 / 使用次数这些绑本机库的字段。"""
    return {
        "kind": tpl.get("kind") or "article",
        "name": tpl.get("name") or "",
        "subtype": tpl.get("subtype") or "",
        "channel": tpl.get("channel") or "",
        "outline": _norm_outline(_json_list(tpl.get("outline_json"))),
        "style_prompt": tpl.get("style_prompt") or "",
        "checks": [str(c) for c in _json_list(tpl.get("checks_json"))],
        "tags": [str(t) for t in _json_list(tpl.get("tags_json"))],
        "note": tpl.get("note") or "",
    }


@router.get("/studio/templates", dependencies=[Depends(verify_key)])
async def list_templates(request: Request, kind: str = "", subtype: str = "",
                         channel: str = "", q: str = ""):
    """模板库。`q` 只看名字；其余是精确过滤（渠道与文种正交，可单独筛）。"""
    return await _store(request).list_templates(
        kind=kind, subtype=subtype, channel=channel, q=(q or "").strip())


@router.post("/studio/templates", dependencies=[Depends(verify_key)])
async def create_template(body: TemplateCreate, request: Request):
    """新建模板。界面上一律 is_builtin=0；内置只从 YAML 种子来。"""
    return await _store(request).create_template(
        kind=body.kind,
        name=body.name.strip(),
        subtype=body.subtype,
        channel=body.channel,
        outline=_norm_outline(body.outline),
        style_prompt=body.style_prompt,
        checks=body.checks,
        tags=body.tags,
        note=body.note,
    )


@router.post("/studio/templates/import", dependencies=[Depends(verify_key)])
async def import_template(payload: dict[str, Any], request: Request):
    """导入一份导出的模板 JSON。

    一律落成新模板（`is_builtin=0`），不按原 id 覆盖：导出文件是拿来换机或
    发给别人的，覆盖掉本机同 id 的模板等于悄悄改别人的库。
    """
    tpl = payload.get("template") if isinstance(payload.get("template"), dict) else payload
    name = str(tpl.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板缺 name，认不出这是什么")
    kind = str(tpl.get("kind") or "article")
    if kind not in ("article", "novel"):
        raise HTTPException(status_code=400, detail=f"kind 只认 article / novel: {kind}")
    outline = _norm_outline(tpl.get("outline"))
    if not outline:
        raise HTTPException(status_code=400, detail="模板骨架是空的：没有 outline 就没有可套的东西")
    return await _store(request).create_template(
        kind=kind,
        name=name[:80],
        subtype=str(tpl.get("subtype") or ""),
        channel=str(tpl.get("channel") or ""),
        outline=outline,
        style_prompt=str(tpl.get("style_prompt") or ""),
        checks=[str(c) for c in (tpl.get("checks") or [])],
        tags=[str(t) for t in (tpl.get("tags") or [])],
        note=str(tpl.get("note") or ""),
    )


@router.post("/studio/templates/from-work/{work_id}", dependencies=[Depends(verify_key)])
async def template_from_work(work_id: str, body: TemplateFromWork, request: Request):
    """把一本作品已写好的骨架沉淀成模板。

    只用「已经写出来的东西」反推骨架，不调模型：节标题就是 heading，
    节拍卡（或正文开头）就是 prompt，实际字数就是 target_words。
    这样改一版骨架的成本是「正常写一篇」，而不是「先去学写模板」。
    """
    store = _store(request)
    work = await _require_work(request, work_id)
    tree = await store.list_tree(work_id)
    readable = [n for n in tree if n.get("ordinal") is not None]
    readable.sort(key=lambda n: n.get("ordinal") or 0)
    if not readable:
        raise HTTPException(
            status_code=400,
            detail="这本作品还没有可读的节点：先生成大纲或手写几节，再来沉淀骨架",
        )

    truncated = max(0, len(readable) - 30)
    outline: list[dict[str, Any]] = []
    for item in readable[:30]:
        node = await store.get_node(item["id"]) or {}
        heading = (item.get("title") or "").strip() or f"第 {item.get('ordinal')} 节"
        # 节拍卡优先：它是人（或模型）写这一节时的真实意图
        prompt = (node.get("summary") or "").strip()
        if not prompt:
            prompt = re.sub(r"\s+", " ", (node.get("content") or "")).strip()[:120]
        outline.append({
            "heading": heading[:80],
            "prompt": prompt,
            "target_words": int(item.get("word_count") or 0) or 800,
        })

    resolved = content_types.resolve(
        work.get("kind") or "article", work.get("scale") or "",
        work.get("subtype") or "", work.get("channel") or "")
    from app.studio.assembler import _style_text  # 与装配时同一份口径，避免两处漂移
    created = await store.create_template(
        kind=work.get("kind") or "article",
        name=(body.name or "").strip()[:80] or f"{work.get('title') or '未命名'} · 骨架",
        subtype=work.get("subtype") or "",
        channel=work.get("channel") or "",
        outline=outline,
        style_prompt=_style_text(work),
        checks=list(resolved.get("checks") or []),
        tags=body.tags,
        note=body.note,
        source_work_id=work_id,
    )
    return {**created, "truncated": truncated, "source_title": work.get("title") or ""}


@router.get("/studio/templates/{template_id}", dependencies=[Depends(verify_key)])
async def get_template(template_id: str, request: Request):
    tpl = await _store(request).get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    return tpl


@router.patch("/studio/templates/{template_id}", dependencies=[Depends(verify_key)])
async def update_template(template_id: str, body: TemplateUpdate, request: Request):
    store = _store(request)
    tpl = await store.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    if int(tpl.get("is_builtin") or 0):
        raise HTTPException(status_code=409, detail=_BUILTIN_READONLY)
    payload = body.model_dump(exclude_none=True)
    if "outline" in payload:
        payload["outline"] = _norm_outline(payload["outline"])
        if not payload["outline"]:
            raise HTTPException(status_code=400, detail="骨架不能为空：至少留一节")
    if "name" in payload and not payload["name"].strip():
        raise HTTPException(status_code=400, detail="模板名不能为空")
    got = await store.update_template(template_id, **payload)
    if got is None:
        raise HTTPException(status_code=404, detail="模板不存在或无更新")
    return got


@router.delete("/studio/templates/{template_id}", dependencies=[Depends(verify_key)])
async def delete_template(template_id: str, request: Request):
    """删模板并解绑引用它的作品。返回解绑了几本，让前端能说清影响。"""
    store = _store(request)
    tpl = await store.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    if int(tpl.get("is_builtin") or 0):
        raise HTTPException(status_code=409, detail=_BUILTIN_READONLY)
    return await store.delete_template(template_id)


@router.post("/studio/templates/{template_id}/duplicate", dependencies=[Depends(verify_key)])
async def duplicate_template(template_id: str, request: Request):
    """复制一份可改的副本。

    这是改内置模板的唯一入口：内置行每次启动都被 YAML 覆盖，
    直接放开编辑，用户改完重启就没了，比不能改更让人恼火。
    """
    store = _store(request)
    tpl = await store.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    return await store.create_template(
        kind=tpl.get("kind") or "article",
        name=f"{tpl.get('name') or '未命名'}（副本）"[:80],
        subtype=tpl.get("subtype") or "",
        channel=tpl.get("channel") or "",
        outline=_json_list(tpl.get("outline_json")),
        style_prompt=tpl.get("style_prompt") or "",
        checks=[str(c) for c in _json_list(tpl.get("checks_json"))],
        tags=[str(t) for t in _json_list(tpl.get("tags_json"))],
        note=tpl.get("note") or "",
        source_work_id=tpl.get("source_work_id") or "",
    )


@router.post("/studio/templates/{template_id}/default", dependencies=[Depends(verify_key)])
async def set_default_template(template_id: str, request: Request):
    """设为同 kind+subtype+channel 的默认模板（同组其它行会被清零）。"""
    store = _store(request)
    tpl = await store.get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    await store.set_default_template(template_id)
    return await store.get_template(template_id)


@router.post("/studio/templates/{template_id}/pin", dependencies=[Depends(verify_key)])
async def pin_template(template_id: str, request: Request, body: Optional[TemplatePin] = None):
    """收藏 / 取消收藏。不传 body 就是取反。"""
    got = await _store(request).toggle_template_pin(
        template_id, None if body is None else body.pinned)
    if got is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return got


@router.get("/studio/templates/{template_id}/export", dependencies=[Depends(verify_key)])
async def export_template(template_id: str, request: Request):
    """导出成一份可换机、可发人的 JSON。"""
    tpl = await _store(request).get_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"format": TEMPLATE_FORMAT, "template": _template_payload(tpl)}


# 备份失败时回给前端的那一句：固定文案。
# 前端会把 `detail` 直接显示出来，而 `BackupError` 的原话里带着本机库文件与备份目录的位置，
# 那句只该进日志。
_BACKUP_DENIED = "备份失败，已拒绝删除，详见服务端日志"
# 快照失败同一口径：它不是「拒绝了某个破坏性动作」，只是多一份副本没落成。
_SNAPSHOT_FAILED = "快照没写成，详见服务端日志"

# node_versions.source 的取值集合，与前端 SOURCE_LABEL 一一对应。
# 放开成任意字符串的话，历史页会出现「未知」这种没人看得懂的行，回滚前不敢点。
# `bulk_replace` 是「全作品替换」单独开的那一档：替换不是今天新写的字，不该刷高日历。
_VERSION_SOURCES = {"human", "ai_draft", "ai_polish", "bulk_replace"}


# ==================== 作品 ====================

@router.get("/studio/works", dependencies=[Depends(verify_key)])
async def list_works(request: Request, kind: str = "", status: str = "", limit: int = 100):
    return await _store(request).list_works(kind=kind, status=status, limit=limit)


def _hit_snippet(text: str, q: str, width: int = 140) -> str:
    """命中处附近的窗口。原文里找得到就围绕它截，找不到（中文被分词切开了）就取开头。"""
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return ""
    pos = flat.find(q) if q else -1
    start = max(0, pos - width // 3) if pos > 0 else 0
    piece = flat[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")


@router.get("/studio/search", dependencies=[Depends(verify_key)])
async def search_all(request: Request, q: str = "", limit: int = 20):
    """跨作品检索：标题/主旨 + 正文 FTS。

    原先前端只能在某一篇里搜（`POST /studio/works/{id}/search`），作品一多就答不了
    「我之前在哪篇写过这个」。标题命中单独给一组：正文 FTS 不索引标题。
    """
    q = (q or "").strip()[:200]
    if not q:
        return {"query": "", "works": [], "hits": [], "count": 0}
    store = _store(request)
    works = await store.search_works_meta(q, limit=3)
    match = tokenize.match_query(q)
    rows = await store.search_chunks_global(match, limit=max(1, min(limit, 50))) if match else []
    hits: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        # 同一节命中多个块时只留最相关的那一块：这张表要答「去哪几节看」，不是片段全集
        key = (r["work_id"], r.get("node_id") or r.get("source_id") or "")
        if key in seen:
            continue
        seen.add(key)
        hits.append({
            "work_id": r["work_id"],
            "work_title": r.get("work_title") or "",
            "work_status": r.get("work_status") or "",
            "node_id": r.get("node_id") or "",
            "node_title": r.get("node_title") or "",
            "ordinal": int(r.get("ordinal") or 0),
            "source_kind": r.get("source_kind") or "body",
            "snippet": _hit_snippet(r.get("text") or "", q),
        })
    return {"query": q, "works": works, "hits": hits, "count": len(hits)}



@router.post("/studio/works", dependencies=[Depends(verify_key)])
async def create_work(body: WorkCreate, request: Request):
    store = _store(request)
    template_id = body.template_id
    # 没指定模板时按「文种 + 渠道」找一套：优先级链在 store.pick_template 里，
    # 第一条是 subtype+channel 的默认，最后兜底到该 kind 的任意一个。
    # 注意区分「没填」与「空串」：前者是「你帮我挑」，后者是「不套模板」，
    # 两者都当没填的话，界面上那个「不套模板」选项会静默失效。
    # 空串要归成 NULL：这一列是指向 templates 的外键，空串不是任何模板的 id。
    if template_id is None:
        picked = await store.pick_template(body.kind, body.subtype, body.channel)
        if picked:
            template_id = picked["id"]
    elif not template_id.strip():
        template_id = None
    return await store.create_work(
        kind=body.kind,
        scale=body.scale,
        subtype=body.subtype,
        channel=body.channel,
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
        logger.error(f"[studio] 删作品被备份拦住 {work_id}: {exc}")
        raise HTTPException(status_code=500, detail=_BACKUP_DENIED) from exc
    return {"ok": True}


# ==================== 整本搬迁（作品包） ====================

def _bundle_filename(title: str) -> str:
    """包文件名的主干只用安全字符：中文靠 filename* 走 URL 编码。"""
    return re.sub(r'[\\/:*?"<>|]+', "_", title)[:60] or "work"


@router.get("/studio/works/{work_id}/bundle", dependencies=[Depends(verify_key)])
async def export_bundle(work_id: str, request: Request):
    """把一本书导出成 JSON 包（树 + 版本 + 素材 + 图谱），用于换机或存档。"""
    bundle = await _store(request).export_work(work_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"作品不存在: {work_id}")
    title = (bundle["work"] or {}).get("title") or "work"
    body = json.dumps(bundle, ensure_ascii=False)
    encoded = quote(_bundle_filename(title) + ".bundle.json")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=work.bundle.json; filename*=UTF-8''{encoded}",
            "X-Nodes": str(len(bundle["nodes"])),
        },
    )


@router.get("/studio/library/bundle", dependencies=[Depends(verify_key)])
async def export_library(request: Request):
    """整库导出：每本书一个包，外面套一层书目。"""
    data = await _store(request).export_library()
    body = json.dumps(data, ensure_ascii=False)
    encoded = quote("novel-studio-library.bundle.json")
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition":
                f"attachment; filename=library.bundle.json; filename*=UTF-8''{encoded}",
            "X-Works": str(len(data["works"])),
        },
    )


@router.post("/studio/works/import", dependencies=[Depends(verify_key)])
async def import_bundle(payload: dict[str, Any], request: Request):
    """导入单个作品包或整库包：每本都落成一本新书，不按原 id 覆盖。

    一本失败不拦别的：把失败清单一起回给前端，已导进来的照常保留——
    整库导入时因为第三本书缺字段就全都不要，比丢一本更难收拾。
    """
    store = _store(request)
    if payload.get("format") == LIBRARY_FORMAT:
        items = payload.get("works") or []
    elif payload.get("format") == BUNDLE_FORMAT:
        items = [payload]
    else:
        raise HTTPException(status_code=400, detail="不是作品包：format 对不上")
    if not items:
        raise HTTPException(status_code=400, detail="包里没有作品")
    if len(items) > 200:
        raise HTTPException(status_code=400, detail="一次最多导入 200 本")

    imported: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for idx, item in enumerate(items, 1):
        try:
            got = await store.import_work(item)
        except ValueError as exc:
            failed.append({"index": idx, "error": str(exc)})
            continue
        # 索引是派生的：导完重建一次，刚导进来的书立刻能搜、能检索
        try:
            got["chunks"] = (await indexer.reindex_work(store, got["id"]))["chunks_total"]
        except Exception as exc:  # noqa: BLE001 - 建索引失败不该让导入回滚
            logger.warning(f"[studio] 导入后建索引失败 work={got['id']}: {exc}")
            got["chunks"] = 0
        imported.append(got)

    if not imported:
        detail = failed[0]["error"] if failed else "没有可导入的作品"
        raise HTTPException(status_code=400, detail=detail)
    return {"imported": imported, "failed": failed, "count": len(imported)}


@router.get("/studio/works/{work_id}/tree", dependencies=[Depends(verify_key)])
async def get_tree(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await _store(request).list_tree(work_id)


@router.get("/studio/works/{work_id}/stats", dependencies=[Depends(verify_key)])
async def get_stats(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await _store(request).stats(work_id)


class IdeaCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class IdeaUpdate(BaseModel):
    text: Optional[str] = Field(default=None, max_length=500)
    status: Optional[Literal["open", "used", "dropped"]] = None
    work_id: Optional[str] = None


@router.get("/studio/ideas", dependencies=[Depends(verify_key)])
async def list_ideas(request: Request, status: str = "open"):
    """选题池。status 传空串看全部（含已经用掉的）。"""
    return await _store(request).list_ideas(status=status)


@router.post("/studio/ideas", dependencies=[Depends(verify_key)])
async def create_idea(body: IdeaCreate, request: Request):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="想法不能为空")
    return await _store(request).add_idea(text)


@router.patch("/studio/ideas/{idea_id}", dependencies=[Depends(verify_key)])
async def update_idea(idea_id: str, body: IdeaUpdate, request: Request):
    store = _store(request)
    if not await store.update_idea(idea_id, **body.model_dump(exclude_none=True)):
        raise HTTPException(status_code=404, detail="想法不存在或无更新")
    return await store.get_idea(idea_id)


@router.delete("/studio/ideas/{idea_id}", dependencies=[Depends(verify_key)])
async def delete_idea(idea_id: str, request: Request):
    if not await _store(request).delete_idea(idea_id):
        raise HTTPException(status_code=404, detail="想法不存在")
    return {"ok": True}


def streak_days(rows: list[dict[str, Any]], today: date) -> int:
    """连续写作天数。今天还没写不算断，从昨天往回数。"""
    have = {r["day"] for r in rows if int(r.get("chars") or 0) > 0}
    cursor = today if today.isoformat() in have else today - timedelta(days=1)
    n = 0
    while cursor.isoformat() in have:
        n += 1
        cursor -= timedelta(days=1)
    return n


@router.get("/studio/stats/daily", dependencies=[Depends(verify_key)])
async def daily_writes(request: Request, days: int = 30):
    """按天净增字数 + 连续天数。

    数据从 `node_versions` 现算（不额外记一份）：今天还没写不算断，从昨天往回数——
    中午打开看到「连续 6 天」突然变 0，只会让人以为统计坏了。
    """
    rows = await _store(request).daily_writes(days=max(1, min(days, 180)))
    have = {r["day"]: r for r in rows}
    today = date.today()
    today_s = today.isoformat()
    streak = streak_days(rows, today)
    return {
        "days": rows,
        "today": int(have.get(today_s, {}).get("chars") or 0),
        "saves_today": int(have.get(today_s, {}).get("saves") or 0),
        "streak": streak,
        "total": sum(int(r["chars"] or 0) for r in rows),
    }


@router.get("/studio/stats/pipeline", dependencies=[Depends(verify_key)])
async def pipeline_stats(request: Request, days: int = 14):
    """连载节奏：定稿存稿还能日更几天。

    日更速度的口径按用户定的：优先近 N 个**日历天**的实际日均（含今天，
    没写的那天算 0 天，不把自己休息日藏掉）；窗口内一笔没写退回
    每日目标；两个都没有就不编数——days_left 给 null 比编一个假天数诚实。
    """
    days = max(3, min(int(days or 14), 60))
    store = _store(request)
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    got = await store.pipeline_window(since)
    window_chars = sum(int(r["chars"] or 0) for r in got["recent"])
    raw = await store.get_setting("daily_target_words")
    try:
        target = max(0, int(float(raw or 0)))
    except (ValueError, OverflowError):
        # 设置项是原样存的字符串，直接打 API 能写进 "inf"——int(float("inf"))
        # 抛的是 OverflowError，漏捕了这里就 500。认不出就当没设。
        target = 0
    if window_chars > 0:
        pace, source = window_chars / days, "history"
    elif target > 0:
        pace, source = float(target), "target"
    else:
        pace, source = 0.0, None
    # 天数按展示的那个整数节奏除：窗口内只写过几字时 pace 会四舍五入到 0，
    # 若仍拿未取整的浮点去除，界面就摆出「0 字/天 · 几千天可更」的自相矛盾。
    # 不足 1 字/天按 1 兜底——展示与换算从此是同一个数。
    pace_disp = max(1, round(pace)) if pace > 0 else 0
    return {
        "days": days,
        "stock_words": got["stock_words"],
        "stock_nodes": got["stock_nodes"],
        "window_chars": window_chars,
        "pace": pace_disp,
        "pace_source": source,
        "target": target,
        "days_left": int(got["stock_words"] // pace_disp) if pace_disp > 0 else None,
    }


@router.get("/studio/stats/cost-quality", dependencies=[Depends(verify_key)])
async def cost_quality(request: Request, works: int = 50, nodes: int = 50):
    """生成成本与质量：runs 的耗时/字数按作品与按节汇总，附未处理硬告警。

    `works` / `nodes` 只限返回行数，与统计口径无关；均分只算打过分的跑批。
    """
    return await _store(request).cost_quality(work_limit=works, node_limit=nodes)



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
    store = _store(request)
    payload = body.model_dump(exclude_none=True)
    snapshot = bool(payload.pop("snapshot", False))
    source = payload.pop("snapshot_source", None)
    force = bool(payload.pop("force", False))
    # nodes 表没有 run_id 列，必须自己 pop 掉——留在 payload 里会随 **fields 进 UPDATE
    run_id = payload.pop("run_id", None)
    payload.pop("base_updated_at", None)
    payload.pop("ignore_conflict", None)
    if source and source not in _VERSION_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"版本来源不认: {source}（只能是 {'/'.join(sorted(_VERSION_SOURCES))}）",
        )
    plan = payload.get("plan_json")
    clean_plan = _clean_plan(plan) if plan is not None else None
    if clean_plan is not None:
        payload["plan_json"] = json.dumps(clean_plan, ensure_ascii=False)
    node = await store.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    # 冲突闸：只拦改正文的写入，而且只拦带 base 的调用方——没带 base 的（服务端
    # 内部写入、旧调用）无从判断对方见过哪一份，维持原行为。
    # 比的是秒级浮点戳：取到存之间库里被别处写过就 409，把服务端整份带回去
    # 让调用方选重取还是强存；last-write-wins 会把另一边的改动吞得无声无息。
    if ("content" in payload and body.base_updated_at is not None
            and not body.ignore_conflict
            and float(node.get("updated_at") or 0) - body.base_updated_at > 1e-6):
        raise HTTPException(status_code=409, detail={
            "conflict": True,
            "server": node,
        })
    # 定稿闸门：只拦「硬告警」（severity=error），提醒与提示不挡路。
    # 这里不写库、不改状态——前端拿到 409 里那几条，弹一句确认再带 force 重试；
    # 告警本身不会随定稿自动消掉，人在「告警」里点「已处理」才消。
    if payload.get("status") == "final" and not force:
        gate = await _final_gate(store, node)
        if gate["open_errors"]:
            raise HTTPException(status_code=409, detail=gate)
    ok = await store.update_node(
        node_id, snapshot=snapshot, snapshot_source=source or "human",
        run_id=run_id, **payload
    )
    if not ok:
        raise HTTPException(status_code=404, detail="节点不存在或无更新")
    # 计划改了要顺手同步旧的 works.meta_json.outline：导出包、打分与旧代码
    # 还在读那一份，两边不一致时「计划页改了、生成没变」会很难查。
    if plan is not None:
        work = await store.get_work(node["work_id"])
        if work:
            meta = _work_meta(work)
            meta.setdefault("outline", {})[node_id] = {
                "prompt": (clean_plan or {}).get("prompt", ""),
                "target_words": (clean_plan or {}).get("target_words", 0),
            }
            await store.update_work(work["id"], meta_json=json.dumps(meta, ensure_ascii=False))
    return await store.get_node(node_id)


@router.get("/studio/nodes/{node_id}/meta", dependencies=[Depends(verify_key)])
async def node_meta(node_id: str, request: Request):
    """只报时间戳与字数的轻接口：写作台每十几秒摸一次，探「别处有没有动过这一节」。

    一节正文可能几万字，拿全文轮询太贵；这一份够前端判断要不要再去拉全文。
    """
    node = await _store(request).get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    return {k: node.get(k) for k in ("id", "title", "word_count", "status", "updated_at")}


@router.post("/studio/nodes/{node_id}/beats", dependencies=[Depends(verify_key)])
async def regenerate_beats(node_id: str, body: BeatsRequest, request: Request):
    """给这一节重抽节拍卡（章纲），并直接写回 `nodes.summary`。

    与队列里的 `beats` 任务共用同一套 prompt 与清洗逻辑：交互式重抽
    不该产出另一种格式，否则「计划页写的」和「排队写的」会打架。
    """
    store = _store(request)
    node = await store.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    work = await store.get_work(node["work_id"])
    if not work:
        raise HTTPException(status_code=404, detail="作品不存在")
    if (work.get("kind") or "") != "novel":
        raise HTTPException(status_code=400, detail="节拍卡只用于小说")
    meta = _work_meta(work)
    focus = (body.focus or "").strip() or prompting.node_outline(node, meta).get("prompt", "")
    prev = await tasks._prev_summary(store, work["id"], int(node.get("ordinal") or 0))
    known = [e["name"] for e in await store.list_entities(work["id"])]
    system, user = prompting.beats_prompt(
        work, node, prev_hint=prev, focus=focus, known_names=known)
    model = pick_model(request.app.state.registry, body.model or None)
    res = await _gateway(request).complete(
        model=model, system=system, prompt=user, mode="quick")
    beats = prompting.parse_beats(res.text or "")
    if not beats:
        raise HTTPException(status_code=502, detail="节拍卡为空：换个模型再试，或手写")
    await store.update_node(node_id, summary=beats)
    return {"beats": beats, "chars": len(beats)}


@router.get("/studio/nodes/{node_id}/final-gate", dependencies=[Depends(verify_key)])
async def final_gate(node_id: str, request: Request):
    """定稿前看一眼：这一节还有几条硬告警。

    与 PATCH 里的那一层是同一份判断，单独开一个只读接口是为了让前端在
    `confirm` 里把规则与原文摆出来——409 只能带一段话，说不清是哪几条。
    """
    node = await _store(request).get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    return await _final_gate(_store(request), node)


@router.post("/studio/nodes/{node_id}/move", dependencies=[Depends(verify_key)])
async def move_node(node_id: str, body: NodeMove, request: Request):
    """同层上下移一节，与返回体里 `swapped_with_title` 那一节互换位置。

    不报 404 也不报 400：「已到边界」是正常分支（树上的首/末行点了上下移），
    前端按 `moved` 判断，把 `reason` 当提示语显示就行。
    """
    return await _store(request).move_node(node_id, body.direction)


@router.post("/studio/nodes/{node_id}/reparent", dependencies=[Depends(verify_key)])
async def reparent_node(node_id: str, body: NodeReparent, request: Request):
    """把一节拖进另一卷末尾（`parent_id` 为 null 则拖回顶层），或插到某节之前。

    `before_id` 非空时按「放到这一行前面」处理，落点由那个节点的父级决定。
    与 move 不同，这里的不合法多数是误拖（拖到叶子上、拖进自己、跨作品）：
    404 表示节点或目标不存在，400 带一句人看得懂的原因，前端直接显示。
    """
    try:
        result = await _store(request).reparent_node(
            node_id, body.parent_id, before_id=body.before_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="节点或目标不存在")
    return result


@router.post("/studio/nodes/{node_id}/duplicate", dependencies=[Depends(verify_key)])
async def duplicate_node(node_id: str, request: Request):
    """复制这一节连同整棵子树，插到原节紧后方。

    只复制树上的壳（正文/节拍卡/计划/状态），版本历史与索引不跟。
    新节需要前端重新 select：章号会随树序重排，与 reparent 同一口径。
    """
    try:
        result = await _store(request).duplicate_node(node_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    return result


@router.post("/studio/nodes/{node_id}/split", dependencies=[Depends(verify_key)])
async def split_node(node_id: str, body: NodeSplit, request: Request):
    """按字符偏移把一节的正文切成若干节，新节紧跟在原节后面。

    切割点由前端在编辑器里选（光标位置即切点），后端只验越界与顺序。
    offsets 为空什么也不改（`split: false`），方便前端「没选切点就点拆分」给提示。
    """
    try:
        result = await _store(request).split_node(node_id, body.offsets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    return result


@router.post("/studio/nodes/{node_id}/merge", dependencies=[Depends(verify_key)])
async def merge_node(node_id: str, body: NodeMerge, request: Request):
    """把连续几节合并成一节：正文拼进第一节，其余节先落备份再删。

    被吸收的节删前会像单删一样写节点备份（在 storage/backups/nodes/），
    备份写不成为 500；只吃叶子节，遇到容器或带子节点的节是 400。
    """
    store = _store(request)
    try:
        result = await store.merge_nodes(node_id, count=body.count,
                                         separator=body.separator)
    except BackupError as exc:
        logger.error(f"[studio] 合并被备份拦住 {node_id}: {exc}")
        raise HTTPException(status_code=500, detail=_BACKUP_DENIED) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    return result


@router.delete("/studio/nodes/{node_id}", dependencies=[Depends(verify_key)])
async def delete_node(node_id: str, request: Request):
    try:
        ok = await _store(request).delete_node(node_id)
    except BackupError as exc:
        logger.error(f"[studio] 删节点被备份拦住 {node_id}: {exc}")
        raise HTTPException(status_code=500, detail=_BACKUP_DENIED) from exc
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


# ==================== 删除备份与每日快照 ====================

@router.get("/studio/backups", dependencies=[Depends(verify_key)])
async def list_backups(request: Request):
    """列出备份：删除前的节点 JSON 与整库副本，加上每天的整库快照。只读。"""
    store = _store(request)
    return {
        "nodes": store.list_node_backups(),
        "works": store.list_work_backups(),
        "snapshots": store.list_snapshot_backups(),
        # 前端拿这个数说「留最近 N 份」，不让它自己再写一份常量
        "snapshot_keep": BACKUP_SNAPSHOT_KEEP,
        # 只给「相对存储目录」的写法：绝对路径会原样显示到界面上，把部署位置说出去。
        # 想看完整位置用 `scripts/restore_backup.py --list`，那才是给人用的入口。
        "dir": f"{store.db_path.parent.name}/backups/nodes",
        # 快照与删除副本是两个目录，不给这一行前端就只能说一个地方
        "snapshot_dir": f"{store.db_path.parent.name}/backups/snapshots",
    }


@router.post("/studio/backups/snapshot", dependencies=[Depends(verify_key)])
async def take_snapshot(request: Request):
    """立刻取一份整库快照：写一大段之前先存一份，不用等到明天。

    与每日快照同一个目录、同一条保留策略（两类一起数 N 份），所以手动按多了也会
    挤掉最旧的那一份——这是有意的：留的是「最近的几份」，不是「每天的那一份」。
    """
    store = _store(request)
    try:
        dest = await store.snapshot_now()
    except BackupError as exc:
        logger.error(f"[studio] 手动快照失败: {exc}")
        raise HTTPException(status_code=500, detail=_SNAPSHOT_FAILED) from exc
    st = dest.stat()
    return {"name": dest.name, "bytes": st.st_size, "created_at": st.st_mtime}


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


# ==================== DOCX 导入 ====================

@router.post("/studio/import/docx", dependencies=[Depends(verify_key)])
async def import_docx(request: Request):
    """把 .docx 解成纯文本（不写库）：前端拿去填进导入弹层，预览、拆节、落库都走老路。

    收原始字节而不是 multipart：那样要新增 python-multipart 依赖，而这一份本来就是
    单机内网的小工具接口。解析只用标准库（见 `app.studio.docx_import`）。
    """
    data = await request.body()
    try:
        text = docx_to_text(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not text:
        raise HTTPException(status_code=400, detail="这份 docx 里没有可读的文字")
    return {
        "text": text,
        "chars": len(text),
        # 带井号的标题行数：前端拿来提示「按标题拆能不能拆得开」
        "headings": sum(1 for line in text.splitlines() if line.startswith("#")),
    }


# ==================== 素材 / 事实 / 术语 ====================

@router.get("/studio/works/{work_id}/sources", dependencies=[Depends(verify_key)])
async def list_sources(work_id: str, request: Request, limit: int = 100):
    await _require_work(request, work_id)
    return await _store(request).list_sources(work_id, limit=limit)


@router.post("/studio/works/{work_id}/sources", dependencies=[Depends(verify_key)])
async def add_source(work_id: str, body: SourceCreate, request: Request):
    await _require_work(request, work_id)
    store = _store(request)
    created = await store.add_source(
        work_id,
        content=body.content,
        title=body.title,
        url=body.url,
        kind=body.kind,
        published_at=body.published_at,
    )
    # 导入即建索引：不建的话 S_src 只能退回「最近 5 条的开头」，
    # 刚导进来的资料在第一次生成时压根不会被按内容选中。
    try:
        full = await store.get_source(created["id"])
        if full:
            await indexer.index_source(store, full)
    except Exception as exc:  # noqa: BLE001 - 建索引失败不该让导入失败
        logger.warning(f"[studio] 素材索引失败 id={created['id']}: {exc}")
    return created


@router.get("/studio/sources/{source_id}", dependencies=[Depends(verify_key)])
async def get_source(source_id: str, request: Request):
    """单条素材全文：列表不带正文（一条可能几万字），点开再看。"""
    src = await _store(request).get_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="素材不存在")
    return src


@router.patch("/studio/sources/{source_id}", dependencies=[Depends(verify_key)])
async def update_source(source_id: str, body: SourceUpdate, request: Request):
    """改素材标题/出处/正文。

    正文一改，旧的 chunks 还在按老文字做检索——所以这里顺手重建这一条的索引。
    只改标题时 `index_source` 会按 content_hash 自己跳过，不白跑分词。
    """
    store = _store(request)
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="没有可更新字段")
    if "content" in payload and not payload["content"].strip():
        raise HTTPException(status_code=400, detail="正文不能为空：不想留这条请直接删")
    row = await store.update_source(source_id, **payload)
    if row is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    if "content" in payload:
        try:
            await indexer.index_source(store, row)
        except Exception as exc:  # noqa: BLE001 - 建索引失败不该让保存失败
            logger.warning(f"[studio] 素材改后索引失败 id={source_id}: {exc}")
    return row


@router.delete("/studio/sources/{source_id}", dependencies=[Depends(verify_key)])
async def delete_source(source_id: str, request: Request):
    ok = await _store(request).delete_source(source_id)
    if not ok:
        raise HTTPException(status_code=404, detail="素材不存在")
    return {"ok": True}


@router.post("/studio/sources/{source_id}/facts/extract", dependencies=[Depends(verify_key)])
async def extract_source_facts(source_id: str, body: FactsExtractRequest, request: Request):
    """从一条素材里抽事实卡。

    素材是原文，事实卡是「一句可引用的话」。原来这两层之间只能靠人手工抄，
    抄不动就等于没有；抽出来的卡带上 source_id，`citation_missing` / `fact_stale`
    这类检查才追得到出处。
    """
    store = _store(request)
    src = await store.get_source(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="素材不存在")
    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    system, prompt = prompting.facts_prompt(src, limit=max(1, min(body.limit or 5, 10)))
    res = await gateway.complete(model=model, system=system, prompt=prompt, mode="quick")
    items = prompting.parse_facts(res.text or "")
    if not items:
        raise HTTPException(status_code=400, detail="模型没有给出能核对的事实，换条素材或重试")
    created = []
    for it in items:
        created.append(await store.add_fact(
            src["work_id"],
            statement=it["statement"],
            source_id=source_id,
            valid_until=it["valid_until"],
        ))
    return {"ok": True, "model": model, "added": len(created), "facts": created}


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


@router.patch("/studio/facts/{fact_id}", dependencies=[Depends(verify_key)])
async def update_fact(fact_id: str, body: FactUpdate, request: Request):
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="没有可更新字段")
    if "statement" in payload and not payload["statement"].strip():
        raise HTTPException(status_code=400, detail="陈述不能为空")
    row = await _store(request).update_fact(fact_id, **payload)
    if row is None:
        raise HTTPException(status_code=404, detail="事实不存在")
    return row


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


@router.patch("/studio/terms/{term_id}", dependencies=[Depends(verify_key)])
async def update_term(term_id: str, body: TermUpdate, request: Request):
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="没有可更新字段")
    if "term" in payload and not payload["term"].strip():
        raise HTTPException(status_code=400, detail="术语不能为空")
    try:
        row = await _store(request).update_term(term_id, **payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="术语不存在")
    return row


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

    template = await _template_of(store, work)
    resolved = content_types.resolve(work.get("kind") or "article",
                                    work.get("scale") or "", work.get("subtype") or "",
                                    work.get("channel") or "")

    # 与「生成」共用一条指令口径，预览显示的就是真要发出去的那一份
    instruction = _instruction_for(body.task_type, node, work, body.instruction)
    result = await assemble(
        store, work, node, body.task_type, instruction, template=template, resolved=resolved
    )
    payload = result.as_dict()
    # 把「为什么引用了这几段」一并带上，调 prompt 时不用再去翻库
    query = retrieval.build_query(
        node.get("title") if node else "", instruction, work.get("premise")
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
    if body.task_type == "polish" and not (node.get("content") or "").strip():
        # 与队列侧 run_polish 同一条底线：没有正文就没有可润色的东西。
        # 少这一句，界面上拿空节润色会一路走到装配，模型只看到骨架，凭空调出一节。
        raise HTTPException(
            status_code=400, detail="润色需要已有正文：这一节还是空的，先写正文或切到「写本节」"
        )
    instruction = _instruction_for(body.task_type, node, work, body.instruction)

    template = await _template_of(store, work)
    resolved = content_types.resolve(work.get("kind") or "article",
                                    work.get("scale") or "", work.get("subtype") or "",
                                    work.get("channel") or "")

    # 单节生成前把本节最新正文入索引：用户刚手改过，不重建的话
    # S5 检索到的还是旧版本，续写就会接不上。
    try:
        await indexer.index_node(store, node)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[studio] 生成前索引失败 node={node_id}: {exc}")

    result = await assemble(
        store, work, node, body.task_type, instruction, template=template, resolved=resolved
    )
    if not result.user.strip():
        raise HTTPException(status_code=400, detail="上下文为空：请先填写作品主旨或节点正文")
    # 到这里 prompt 就一定要发出去了，素材用量记在此处（预览那条路不记）
    await record_used_sources(store, result)

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
        stopped = False  # 客户端点「停止」：连接断了，收尾时不能再往里写
        yield _sse({
            "event": "meta",
            "run_id": run_id,
            "model": model,
            "task_type": body.task_type,
            "buckets": [b.as_dict() for b in result.buckets],
            "warnings": result.warnings,
            "used_sources": result.used_sources,
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
        except BaseException:  # noqa: BLE001 - 中止走这条：GeneratorExit / CancelledError
            # 生成器已经被关掉，这里再 yield 会抛「async generator ignored GeneratorExit」；
            # 但 run 必须收尾，否则 runs 里永远停在 running，成本统计跟着失真。
            stopped = True
            raise
        finally:
            text = "".join(parts)
            elapsed = int((time.perf_counter() - started) * 1000)
            try:
                await store.finish_run(
                    run_id,
                    status="stopped" if stopped else ("ok" if not error else "failed"),
                    output_chars=len(text),
                    error="客户端中止" if stopped else error,
                    elapsed_ms=elapsed,
                )
            except Exception as exc:  # noqa: BLE001 - 收尾失败不能盖掉真正的中止
                logger.warning(f"[studio] run 收尾失败 run={run_id}: {exc}")
            # 只有明确要求保存时才写库；前端先展示、用户点「采用」再落，避免污染原稿
            # 中止时半截稿不写库：那会把原稿替换成一段没写完的话
            if body.save and text and not error and not stopped:
                content = (node.get("content") or "") if body.task_type == "continue" else ""
                merged = (content + ("\n\n" if content else "") + text) if content else text
                # 落库的也是 AI 写的内容，来源按任务标：润色那一版混进「AI 草稿」里，
                # 人在历史页就分不出哪一版只改了语言、哪一版重写了内容。
                await store.update_node(
                    node_id, content=merged, snapshot=True,
                    snapshot_source=("ai_polish" if body.task_type == "polish" else "ai_draft"),
                    run_id=run_id,
                )
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
                                _work.get("scale") or "", _work.get("subtype") or "",
                                _work.get("channel") or "")
                            await consistency.run_checks(store, _work, fresh, resolved=_resolved)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[studio] 检查失败 node={node_id}: {exc}")
            if not stopped:
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
    """前端下拉框的数据源。新增文种/渠道只改 content-types.yaml，页面不用动。"""
    return {
        "kinds": content_types.list_kinds(),
        # 渠道是独立于文种的一层：前端要选「历史 + 公众号」，两个下拉各取一处
        "channels": content_types.list_channels(),
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
    return await store.get_template(work["template_id"])


def _work_meta(work: dict) -> dict[str, Any]:
    try:
        meta = json.loads(work.get("meta_json") or "{}")
        return meta if isinstance(meta, dict) else {}
    except json.JSONDecodeError:
        return {}


def _clean_plan(plan: Any) -> dict[str, Any]:
    """把前端提交的计划收成稳定形状：四个字符串 + 一个非负整数。

    多出来的键丢掉——`plan_json` 会被拼进 prompt，不能让前端顺手塞别的字段。
    """
    if not isinstance(plan, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("prompt", "pov", "time", "place"):
        val = plan.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()[:2000]
    try:
        target = int(plan.get("target_words") or 0)
    except (TypeError, ValueError):
        target = 0
    if target > 0:
        out["target_words"] = min(target, 100000)
    return out


def _instruction_for(task_type: str, node: Optional[dict], work: dict, extra: str) -> str:
    """起草类任务的指令口径：大纲 / 整篇 / 队列都过 node_instruction，交互式以前是裸传的。

    裸传的后果是提示词一留空，S8 整块不出现（`_head("")` 空文本会被拼装那步跳过），
    模型只拿到模板骨架与主旨，连要写哪一节、写多少字都不知道。
    预览接口也必须走这里：右栏「上下文」若显示的不是真要发出去的那一份，调 prompt 白调。
    """
    if task_type in ("article_draft", "draft") and node:
        return _node_instruction(node, _work_meta(work), extra)
    return extra


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
    # 预估超预算时强行开跑（默认拦下；拦的是手滑，不拦清醒的决定）
    ignore_budget: bool = False
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
    if resolved.get("channel_label"):
        # 渠道影响节数与每节字数：公众号 5 节太长，小红书 5 节太多
        lines.append(f"渠道：{resolved['channel_label']}")
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
                                    work.get("scale") or "", work.get("subtype") or "",
                                    work.get("channel") or "")
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
    # 新计划字段也写一份：右栏「计划」页读的是 nodes.plan_json，
    # 老代码与导出包继续读上面的 meta_json.outline。
    for node_id, info in outline_meta.items():
        await store.update_node(
            node_id, plan_json=json.dumps(_clean_plan(info), ensure_ascii=False))

    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "run_id": last_run_id,
        "nodes": [{"id": n["id"], "title": n["title"], "kind": n["kind"],
                   "ordinal": n.get("ordinal")} for n in created],
        "sections": len(outline_meta),
        "elapsed_ms": elapsed,
        "raw": last_text[:600],
    }


@router.post("/studio/works/{work_id}/pipeline/estimate", dependencies=[Depends(verify_key)])
async def estimate_pipeline(work_id: str, body: PipelineRequest, request: Request):
    """批量生成前的成本预估：和 run_pipeline 同一套选节与选模型口径，纯读不开跑。

    前端把「点批量」改成「先看这一眼预估再确认」；超预算时开跑会吃 402，
    这里提前把同一个数摆出来，不让人在 SSE 开到一半才发现要超支。
    """
    store = _store(request)
    await _require_work(request, work_id)
    nodes = await store.list_readable(work_id, only_empty=body.only_empty)
    if body.limit > 0:
        nodes = nodes[: body.limit]
    model = pick_model(request.app.state.registry, body.model or None)
    est = await cost.estimate_pipeline(
        store, request.app.state.registry, work_id=work_id, sections=len(nodes), model=model,
        spent_before=await _spent_on_model(request, store, work_id, model),
    )
    return est


async def _spent_on_model(request: Request, store, work_id: str, model: str) -> float:
    """这本书已经花掉的钱：成功 runs 的产出字数，按本次目标通道的单价折算。

    口径要说实话：只按产出发、输入侧不计（历史 prompt 构成和这次不一样，
    硬折算反而假）；单价取的是「这次要走的通道」——换通道后拿旧花费
    抵新额度本就不该精确，预估体系宁可粗而不装细。反代不计钱，直接 0。
    """
    price = cost.model_price(request.app.state.registry, model)
    if price is None:
        return 0.0
    chars = await store.output_chars_total(work_id)
    return chars / cost.CHARS_PER_TOKEN / 1000.0 * price["out_per_1k"]


def _budget_402(est: dict) -> HTTPException:
    """拦下时的说法：把「已花 + 本次」一起报——只报本次会让人以为额度是每次的。"""
    return HTTPException(
        status_code=402,
        detail=(
            f"预估花 {est['est_cost']}，加上已花的 {est.get('spent_before', 0)} 共 "
            f"{est.get('projected_total', est['est_cost'])}，超预算上限 {est['budget_limit']}："
            "改小范围（limit）、调高预算，或确认后带 ignore_budget 重跑"
        ),
    )


class BudgetUpdate(BaseModel):
    limit: float = Field(ge=0)


@router.get("/studio/budget", dependencies=[Depends(verify_key)])
async def get_budget(request: Request):
    return {"limit": await cost.budget_limit(_store(request))}


@router.put("/studio/budget", dependencies=[Depends(verify_key)])
async def put_budget(body: BudgetUpdate, request: Request):
    """预算上限（钱数）；0 = 不设限。只拦计钱通道，反代不计钱拦不到。"""
    try:
        await cost.set_budget_limit(_store(request), body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "limit": float(body.limit)}


@router.post("/studio/works/{work_id}/pipeline", dependencies=[Depends(verify_key)])
async def run_pipeline(work_id: str, body: PipelineRequest, request: Request):
    """逐节生成：每节独立装配、独立落库，中途某个失败不影响已完成的部分。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    resolved = content_types.resolve(work.get("kind") or "article",
                                    work.get("scale") or "", work.get("subtype") or "",
                                    work.get("channel") or "")
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

    # 预算闸门：计钱的通道（compat）估超了就不开跑；要硬跑请带 ignore_budget
    est = await cost.estimate_pipeline(
        store, request.app.state.registry, work_id=work_id, sections=len(nodes), model=model,
        spent_before=await _spent_on_model(request, store, work_id, model),
    )
    if est["over_budget"] and not body.ignore_budget:
        raise _budget_402(est)

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


async def _readable_sections(store, work: dict, include_empty: bool) -> list[dict[str, Any]]:
    """按全序取可读节点（带正文）。

    Markdown 导出、公众号 HTML、发布检查三处都从这里取，避免「导出的顺序」
    和「检查的顺序」各算一套——那种漂移查起来最费时间。
    """
    tree = await store.list_tree(work["id"])
    by_id = {n["id"]: n for n in tree}
    readable = [n for n in tree if n.get("ordinal") is not None]
    readable.sort(key=lambda n: n.get("ordinal") or 0)

    out: list[dict[str, Any]] = []
    for item in readable:
        node = await store.get_node(item["id"])
        content = ((node or {}).get("content") or "").strip()
        if not content and not include_empty:
            continue
        parent = by_id.get(item.get("parent_id")) or {}
        out.append({
            "id": item["id"],
            "title": item["title"],
            "content": content,
            "parent_id": parent.get("id") or "",
            "parent_title": parent.get("title") or "",
        })
    return out


async def _render_markdown(store, work: dict, include_empty: bool) -> tuple[str, int]:
    sections = await _readable_sections(store, work, include_empty)
    blocks: list[str] = []
    last_container = None
    for sec in sections:
        if sec["parent_id"] and sec["parent_id"] != last_container:
            blocks.append(f"## {sec['parent_title']}")
            last_container = sec["parent_id"]
        blocks.append(f"### {sec['title']}")
        if sec["content"]:
            blocks.append(sec["content"])

    if not blocks:
        return "", 0
    markdown = f"# {work['title']}\n\n" + "\n\n".join(blocks) + "\n"
    return markdown, len(sections)


@router.post("/studio/works/{work_id}/assemble", dependencies=[Depends(verify_key)])
async def assemble_document(work_id: str, request: Request, include_empty: bool = False):
    """把各节按全序拼成一份 Markdown。不调用模型，纯拼装。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    markdown, used = await _render_markdown(store, work, include_empty)
    return {"markdown": markdown, "chars": len(markdown), "nodes": used}


@router.get("/studio/works/{work_id}/reading", dependencies=[Depends(verify_key)])
async def reading_view(work_id: str, request: Request, include_empty: bool = False):
    """整本阅读模式的数据：按全序的节列表，不拼成字符串。

    阅读页要画目录锚点与逐节渲染，拆 markdown 字符串反推结构是最容易漂的做法；
    服务端本来就有这份列表（导出/发布检查同源），直接给。
    """
    store = _store(request)
    work = await _require_work(request, work_id)
    sections = await _readable_sections(store, work, include_empty)
    return {
        "title": work.get("title") or "",
        "kind": work.get("kind") or "article",
        "digest": work.get("digest") or "",
        "sections": sections,
        "chars": sum(len(s["content"]) for s in sections),
    }


class DigestRequest(BaseModel):
    model: str = ""
    instruction: str = ""


@router.get("/studio/works/{work_id}/export", dependencies=[Depends(verify_key)])
async def export_document(work_id: str, request: Request, include_empty: bool = False,
                          format: Literal["md", "html", "txt", "docx", "epub"] = "md",
                          theme: str = "", footer: bool = True, archive: bool = True,
                          author: str = ""):
    """导出：md 存档，html 是公众号富文本，txt/docx/epub 是交给编辑与阅读器的成稿。

    公众号编辑器不认 Markdown：粘 `.md` 进去只会看到一堆井号。`format=html`
    这一份可以直接写进剪贴板，粘到公众号后台带格式。

    `theme` 空串表示用「设置里存的默认版式」；`footer` / `archive` 可以关，
    用于只想拿一份干净正文的场合。`author` 只写进 docx/epub 的元数据。
    """
    store = _store(request)
    work = await _require_work(request, work_id)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", work["title"])[:60] or "draft"

    if format in ("txt", "docx", "epub"):
        sections = await _readable_sections(store, work, include_empty)
        if not sections:
            raise HTTPException(status_code=400, detail="还没有可导出的内容")
        if format == "txt":
            body = exporters.render_txt(work, sections, author=author).encode("utf-8")
            media = "text/plain; charset=utf-8"
        elif format == "docx":
            body = exporters.render_docx(work, sections, author=author)
            media = ("application/vnd.openxmlformats-officedocument."
                     "wordprocessingml.document")
        else:
            body = exporters.render_epub(work, sections, author=author)
            media = "application/epub+zip"
        encoded = quote(f"{safe}.{format}")
        return Response(
            content=body,
            media_type=media,
            headers={
                "Content-Disposition": (
                    f"attachment; filename=export.{format}; filename*=UTF-8''{encoded}"),
                "X-Sections": str(len(sections)),
            },
        )

    if format == "html":
        sections = await _readable_sections(store, work, include_empty)
        if not sections:
            raise HTTPException(status_code=400, detail="还没有可导出的内容")
        theme_id, foot, past = await _wechat_extras(store, work, theme, footer, archive)
        page = wechat.render_article(sections, theme=theme_id, footer=foot, archive=past)
        encoded_html = quote(f"{safe}.html")
        return PlainTextResponse(
            content=page,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=export.html; filename*=UTF-8''{encoded_html}",
                "X-Sections": str(len(sections)),
            },
        )

    markdown, used = await _render_markdown(store, work, include_empty)
    if not markdown:
        raise HTTPException(status_code=400, detail="还没有可导出的内容")
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


@router.get("/studio/works/{work_id}/publish-check", dependencies=[Depends(verify_key)])
async def publish_check(work_id: str, request: Request):
    """发布前体检：排版、摘要、风险词。全部本地判，不调模型、不写库。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    sections = await _readable_sections(store, work, include_empty=True)
    theme_id, foot, past = await _wechat_extras(store, work, "", True, True)
    return {
        "items": publish.check(work, sections, digest=work.get("digest") or ""),
        "stats": wechat.stats(sections),
        "images": wechat.extract_images(sections),
        "digest": work.get("digest") or "",
        # 弹层右侧要能直接改版式与文末，所以这些一起带回来，省一次往返
        "title": work.get("title") or "",
        "theme": theme_id,
        "themes": wechat.THEME_LABELS,
        "footer": foot,
        "archive": past,
        "archive_on": await store.get_setting("footer_archive") == "1",
    }


@router.post("/studio/works/{work_id}/digest", dependencies=[Depends(verify_key)])
async def make_digest(work_id: str, body: DigestRequest, request: Request):
    """让模型写一段公众号摘要（≤120 字），写进 work.digest。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    sections = await _readable_sections(store, work, include_empty=False)
    if not sections:
        raise HTTPException(status_code=400, detail="还没有正文，先写几节再写摘要")
    text = "\n\n".join(f"## {s['title']}\n{s['content']}" for s in sections)[:12000]
    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    system = (
        "你是公众号编辑。只输出一段摘要，不超过 120 字：一段话、不换行、"
        "不加标题、不解释，不要用「本文」「这篇文章」开头。"
    )
    prompt = "\n".join([
        f"<title>{work['title']}</title>",
        f"<premise>{work.get('premise') or ''}</premise>",
        f"<extra>{body.instruction}</extra>" if body.instruction else "",
        f"<text>\n{text}\n</text>",
    ]).strip()
    res = await gateway.complete(model=model, system=system, prompt=prompt, mode="quick")
    digest = re.sub(r"\s+", " ", (res.text or "").strip()).strip("\"'「」")
    digest = digest[:publish.DIGEST_MAX]
    if not digest:
        raise HTTPException(status_code=502, detail="模型没有给出摘要，重试一次或手写")
    await store.update_work(work_id, digest=digest)
    return {"digest": digest, "model": model, "chars": len(digest)}


# ==================== 公众号：文风 / 设置 / 标题 / 转发语 / 台账 ====================

# 设置项白名单：settings 是全局的，写坏一个 key 会静默影响所有作品
# daily_target_words 是作者级日更目标：存稿预警在没历史节奏时拿它当分母
_SETTING_KEYS = {"theme", "footer_text", "footer_archive", "voice_profile",
                 "daily_target_words"}
# 标题上限与公众号后台一致；超过的候选直接丢，别让人点了一个存不进去的标题
_TITLE_CAP = 64
_LINE_NOISE = re.compile(r"^\s*(?:[-*·•]|\d+\s*[.、)）]|[（(]\s*\d+\s*[)）])\s*")


def _title_lines(text: str, limit: int) -> list[str]:
    """把模型输出的「一行一个标题」洗干净：去编号、去引号、去解释行。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = _LINE_NOISE.sub("", raw).strip().strip("\"'“”「」《》")
        if not line or len(line) > _TITLE_CAP:
            continue
        if line.endswith(("：", ":", "。")):
            continue
        if line not in out:
            out.append(line)
        if len(out) >= limit:
            break
    return out


def _share_copies(text: str, limit: int) -> list[dict[str, Any]]:
    """转发语优先按 JSON 解析；模型不听话时退回按行切，别让接口报 502。"""
    out: list[dict[str, Any]] = []
    for item in _as_list(_parse_json_loose(text)):
        if isinstance(item, dict):
            body = str(item.get("text") or item.get("copy") or "").strip()
            angle = str(item.get("angle") or "").strip()
        else:
            body, angle = str(item).strip(), ""
        if not body:
            continue
        out.append({
            "angle": (angle or "转发")[:12],
            "text": body[:200],
            "chars": len(body),
        })
        if len(out) >= limit:
            break
    if out:
        return out
    return [{"angle": "转发", "text": t, "chars": len(t)} for t in _title_lines(text, limit)]


async def _wechat_extras(store, work: dict[str, Any], theme: str,
                         with_footer: bool, with_archive: bool):
    """导出的三件外挂：版式、文末引导、往期推荐。

    版式优先级：显式参数 > 设置里的默认 > paper。传了不认识的主题就退回默认，
    不报错——排错时最不需要的就是「导出失败」。
    """
    theme_id = theme if theme in wechat.THEME_LABELS else (await store.get_setting("theme"))
    if theme_id not in wechat.THEME_LABELS:
        theme_id = wechat.DEFAULT_THEME
    foot = (await store.get_setting("footer_text")) if with_footer else ""
    past: list[dict[str, Any]] = []
    if with_archive and await store.get_setting("footer_archive") == "1":
        past = await store.archive_links(
            subtype=work.get("subtype") or "", exclude_work_id=work["id"]
        )
    return theme_id, foot, past


class VoiceSampleCreate(BaseModel):
    title: str = ""
    # 太短的样本统计不出东西：三五十字连句长都不稳
    content: str = Field(min_length=30, max_length=60000)


class VoiceProfileUpdate(BaseModel):
    profile: str = Field(default="", max_length=2000)


class SettingUpdate(BaseModel):
    value: str = Field(default="", max_length=4000)


class TitlesRequest(BaseModel):
    model: str = ""
    count: int = 6
    instruction: str = ""


class ShareRequest(BaseModel):
    model: str = ""
    count: int = 3


class PublicationCreate(BaseModel):
    published_at: str = ""
    url: str = ""
    reads: int = 0
    likes: int = 0
    followers: int = 0
    note: str = ""


class PublicationUpdate(BaseModel):
    published_at: Optional[str] = None
    url: Optional[str] = None
    reads: Optional[int] = None
    likes: Optional[int] = None
    followers: Optional[int] = None
    note: Optional[str] = None


async def _rebuild_voice(store) -> str:
    """样本变了就重算画像。统计是纯本地的，加一篇重算一次不心疼。"""
    texts = [s["content"] for s in await store.list_voice_texts()]
    profile = voice.build(texts)[:2000]
    await store.set_setting("voice_profile", profile)
    return profile


@router.get("/studio/voice", dependencies=[Depends(verify_key)])
async def get_voice(request: Request):
    store = _store(request)
    samples = await store.list_voice_samples()
    return {
        "samples": samples,
        "profile": await store.get_setting("voice_profile"),
        "chars": sum(int(s.get("chars") or 0) for s in samples),
    }


@router.post("/studio/voice/samples", dependencies=[Depends(verify_key)])
async def add_voice_sample(body: VoiceSampleCreate, request: Request):
    """贴一篇旧文。加完立刻重算画像，不用再点一次「统计」。"""
    store = _store(request)
    row = await store.add_voice_sample(content=body.content, title=body.title)
    return {"sample": row, "profile": await _rebuild_voice(store)}


@router.delete("/studio/voice/samples/{sample_id}", dependencies=[Depends(verify_key)])
async def delete_voice_sample(sample_id: str, request: Request):
    store = _store(request)
    if not await store.delete_voice_sample(sample_id):
        raise HTTPException(status_code=404, detail="样本不存在")
    return {"ok": True, "profile": await _rebuild_voice(store)}


@router.post("/studio/voice/analyze", dependencies=[Depends(verify_key)])
async def analyze_voice(request: Request):
    return {"profile": await _rebuild_voice(_store(request))}


@router.put("/studio/voice/profile", dependencies=[Depends(verify_key)])
async def save_voice_profile(body: VoiceProfileUpdate, request: Request):
    """手改画像。下次加/删样本会被重算覆盖，界面上要写清这一点。"""
    store = _store(request)
    await store.set_setting("voice_profile", body.profile.strip()[:2000])
    return {"profile": body.profile.strip()[:2000]}


@router.get("/studio/settings", dependencies=[Depends(verify_key)])
async def get_settings(request: Request):
    return await _store(request).all_settings()


@router.put("/studio/settings/{key}", dependencies=[Depends(verify_key)])
async def put_setting(key: str, body: SettingUpdate, request: Request):
    if key not in _SETTING_KEYS:
        raise HTTPException(status_code=400, detail=f"不支持的设置项: {key}")
    store = _store(request)
    await store.set_setting(key, body.value)
    return {"key": key, "value": body.value}


@router.post("/studio/works/{work_id}/titles", dependencies=[Depends(verify_key)])
async def make_titles(work_id: str, body: TitlesRequest, request: Request):
    """按全文出候选标题。列表页只露前 20 字，所以每条都把字数带回去。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    sections = await _readable_sections(store, work, include_empty=False)
    if not sections:
        raise HTTPException(status_code=400, detail="还没有正文，先写几节再换标题")
    n = max(1, min(int(body.count or 6), 10))
    text = "\n\n".join(f"## {s['title']}\n{s['content']}" for s in sections)[:12000]
    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    system = "你是公众号编辑。只输出候选标题，一行一个，不编号、不加书名号、不解释。"
    prompt = "\n".join([
        f"<title>{work['title']}</title>",
        f"<premise>{work.get('premise') or ''}</premise>",
        f"<extra>{body.instruction}</extra>" if body.instruction else "",
        f"<ask>给 {n} 个备选标题，每个不超过 20 字，角度要分开：数字、反常识、痛点、"
        "对比、具体场景、结论各一个。不用「震惊」「必看」「一文读懂」「干货」，"
        "不玩梗、不押韵、不用感叹号。</ask>",
        f"<text>\n{text}\n</text>",
    ]).strip()
    res = await gateway.complete(model=model, system=system, prompt=prompt, mode="quick")
    titles = _title_lines(res.text, n)
    if not titles:
        raise HTTPException(status_code=502, detail="模型没有给出能用的标题，重试一次")
    return {"titles": [{"text": t, "chars": len(t)} for t in titles], "model": model}


@router.post("/studio/works/{work_id}/share", dependencies=[Depends(verify_key)])
async def make_share(work_id: str, body: ShareRequest, request: Request):
    """朋友圈/群里的转发语。摘要服务公众号列表页，转发语是另一套口气。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    sections = await _readable_sections(store, work, include_empty=False)
    if not sections:
        raise HTTPException(status_code=400, detail="还没有正文，先写几节再写转发语")
    n = max(1, min(int(body.count or 3), 5))
    text = "\n\n".join(f"## {s['title']}\n{s['content']}" for s in sections)[:6000]
    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    system = "你是公众号作者。只输出 JSON 数组，不要解释、不要代码围栏。"
    prompt = "\n".join([
        f"<title>{work['title']}</title>",
        f"<digest>{work.get('digest') or ''}</digest>",
        f"<ask>写 {n} 条转发到朋友圈/微信群的文案，每条不超过 80 字，口气分开："
        "提问式（把读者拉进来）、结论式（直接给判断）、自嘲或坦白式。"
        "不要 emoji，不要「干货」「必看」「速看」「震惊」。</ask>",
        '<format>[{"angle":"提问","text":"..."}]</format>',
        f"<text>\n{text}\n</text>",
    ]).strip()
    res = await gateway.complete(model=model, system=system, prompt=prompt, mode="quick")
    return {"copies": _share_copies(res.text, n), "model": model}


@router.get("/studio/works/{work_id}/publications", dependencies=[Depends(verify_key)])
async def list_publications(work_id: str, request: Request):
    await _require_work(request, work_id)
    return await _store(request).list_publications(work_id)


@router.post("/studio/works/{work_id}/publications", dependencies=[Depends(verify_key)])
async def add_publication(work_id: str, body: PublicationCreate, request: Request):
    await _require_work(request, work_id)
    return await _store(request).add_publication(
        work_id,
        published_at=body.published_at.strip(),
        url=body.url.strip(),
        reads=body.reads,
        likes=body.likes,
        followers=body.followers,
        note=body.note.strip(),
    )


@router.patch("/studio/publications/{pub_id}", dependencies=[Depends(verify_key)])
async def update_publication(pub_id: str, body: PublicationUpdate, request: Request):
    store = _store(request)
    if not await store.get_publication(pub_id):
        raise HTTPException(status_code=404, detail="发布记录不存在")
    await store.update_publication(pub_id, **body.model_dump(exclude_none=True))
    return await store.get_publication(pub_id)


@router.get("/studio/publications/insight", dependencies=[Depends(verify_key)])
async def publications_insight(request: Request):
    """发表回流：在手填台账上聚合。阅读数靠人补记，可能不全——这话界面得说。"""
    return await _store(request).publication_insight()


@router.delete("/studio/publications/{pub_id}", dependencies=[Depends(verify_key)])
async def delete_publication(pub_id: str, request: Request):
    if not await _store(request).delete_publication(pub_id):
        raise HTTPException(status_code=404, detail="发布记录不存在")
    return {"ok": True}


@router.get("/studio/publications/summary", dependencies=[Depends(verify_key)])
async def publication_summary(request: Request):
    return await _store(request).publication_summary()


# ==================== 加工类任务（多候选） ====================

_TRANSFORM_ACTIONS: dict[str, tuple[str, str]] = {
    "rewrite": ("重写", "在不改变事实与结论的前提下重写这段文字，改善节奏、句长与衔接；不得新增事实。"),
    "condense": ("压缩", "把这段压缩到原文的 40%-60%，保留全部关键事实、数字与专有名词。"),
    "expand": ("扩写", "把这段扩写到原文的 1.8-2.2 倍，补充细节、例子与过渡；不得虚构事实或数据。"),
    "polish": ("润色", "只做语言层面的修改：错别字、病句、重复、标点；不改事实、结构与顺序。"),
    "translate": ("翻译", "把这段译成目标语言，术语保持一致，保留原文的段落划分。"),
    "split": ("拆分", "把这段按语义拆成 2-4 个带小标题的段落，内容不增不减。"),
    "merge": ("合并", "把这段合并成一段连贯的话，去掉重复表述，不丢信息。"),
    # 起标题要的不是「加工后的整段」，而是能挑：产出五行候选，界面上逐行回填。
    # 微信那套惊叹号写法先禁掉，否则模型给的每一条都像转载。
    "title": ("起标题", "读这段正文给 5 个备选标题：每个不超过 20 字，直给这段的信息点，"
              "不用「震惊」「必看」「一文读懂」「干货」这类词，不玩梗、不押韵。一行一个，不编号。"),
}


class TransformRequest(BaseModel):
    text: str = Field(min_length=1, max_length=60000)
    action: str = "rewrite"
    instruction: str = ""
    lang: str = "英文"
    candidates: int = 1
    model: str = ""
    # 跨模型对比：每家各出一条（至多 3），给了就接管 candidates——
    # 同一家抽第二稿的差异，往往不如换一家的手感大。
    # 不在 schema 卡 max_length：去重后可能不足 3，截断交给路由里的 want[:3]
    models: list[str] = Field(default_factory=list)
    # 选区前后的正文（前端组好的窗口）：只用来对齐语气、称谓与时态，不输出
    context: str = Field(default="", max_length=20000)
    mode: str = "quick"
    work_id: str = ""

# 上下文只取窗口不取全节：几万字的前后文既撑爆 prompt，模型也抓不住重点
CONTEXT_MAX = 4000


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
                                            work.get("scale") or "", work.get("subtype") or "",
                                            work.get("channel") or "")
            # 一次性任务只拼一段给 system：与逐节装配不同，这里不需要分桶分层
            style = content_types.requirements_text(resolved)

    gateway = _gateway(request)
    model = pick_model(request.app.state.registry, body.model or None)
    if body.action == "title":
        # 默认那句「只输出加工后的正文」会把标题写成一段散文，输出格式先说死
        system = "你是公众号编辑。只输出标题，一行一个，不编号、不加书名号、不解释。"
    else:
        system = "你是中文编辑，只输出加工后的正文，不解释、不加前后缀。"
    if style:
        system += "\n" + style
    user_parts = [f"<task>{brief}</task>"]
    if body.instruction:
        user_parts.append(f"<extra>{body.instruction}</extra>")
    ctx = (body.context or "").strip()
    if ctx:
        # 不声明这句话，模型会把前后文一起复述出来甚至顺手改掉，写回时变成双重替换
        system += ("\n<context> 是选区前后的正文，只用来对齐语气、称谓与时态；"
                   "不要输出它，也不要改动它。")
        user_parts.append(f"<context>\n{ctx[:CONTEXT_MAX]}\n</context>")
    user_parts.append(f"<text>\n{body.text}\n</text>")
    user = "\n\n".join(user_parts)

    n = max(1, min(int(body.candidates or 1), 3))
    # 对比模型去重保序；空串不算。没给 models 才按 candidates 抽同一家多稿
    want: list[str] = []
    for m in body.models:
        m = (m or "").strip()
        if m and m not in want:
            want.append(m)
    targets = list(enumerate(want[:3])) if want else [(i, model) for i in range(n)]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, m in targets:
        run_id = await store.start_run(
            work_id=body.work_id or "transform",
            node_id=None,
            task_type=f"transform:{body.action}",
            model=m,
            provider=gateway.resolve(m)[0],
            prompt_chars=len(system) + len(user),
            variant=i,
        )
        started = time.perf_counter()
        try:
            res = await gateway.complete(model=m, system=system, prompt=user, mode=body.mode or "quick")
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.perf_counter() - started) * 1000)
            await store.finish_run(run_id, status="failed", error=str(exc), elapsed_ms=elapsed)
            errors.append(f"候选 {i + 1}（{m}）失败：{exc}")
            continue
        elapsed = int((time.perf_counter() - started) * 1000)
        await store.finish_run(run_id, status="ok", output_chars=len(res.text), elapsed_ms=elapsed)
        results.append({
            "variant": i,
            "run_id": run_id,
            "model": m,
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


class ReplaceRequest(BaseModel):
    # 不用 `min_length=1`：空串与「全是空白」是两种情况，都得回一句人看得懂的话，
    # 而不是 422 那种字段校验报错（下面路由里一起拦）
    find: str = Field(default="", max_length=replace.MAX_FIND_CHARS)
    replace: str = Field(default="", max_length=replace.MAX_FIND_CHARS)
    # 范围：整本书，还是只那一节。`node` 这种范围走的是同一个接口，不另开一条路径
    scope: Literal["work", "node"] = "work"
    node_id: Optional[str] = None
    case_sensitive: bool = True
    # 默认只看：一次替换摊到整本书是几十上百节，不该由一次回车发起
    dry_run: bool = True
    # 术语表里同名的那一条跟着改（角色改名时顺手把术语也改对）
    update_terms: bool = True


@router.post("/studio/works/{work_id}/replace", dependencies=[Depends(verify_key)])
async def replace_in_work_api(work_id: str, body: ReplaceRequest, request: Request):
    """全作品查找替换。默认 `dry_run=true`：只看命中什么，不动库。

    不做正则：中文稿里用不上，而一条坏正则在一百万字的库上能跑出不归的耗时。
    真替换之前会把旧文先存进版本表，所以改完发现不对还能在「历史」里逐节回滚。
    """
    store = _store(request)
    await _require_work(request, work_id)
    find = body.find
    if not find.strip():
        raise HTTPException(status_code=400, detail="查找内容不能只有一堆空白")
    if body.scope == "node" and not body.node_id:
        raise HTTPException(status_code=400, detail="scope=node 要传 node_id")
    return await replace.replace_in_work(
        store,
        work_id=work_id,
        find=find,
        replace=body.replace,
        scope=body.scope,
        node_id=body.node_id or "",
        case_sensitive=body.case_sensitive,
        dry_run=body.dry_run,
        update_terms=body.update_terms,
    )


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


class ForeshadowUpdate(BaseModel):
    status: Literal["open", "resolved", "abandoned"] = "resolved"
    # 回收发生在第几节；不传就用它当初埋下的节号
    ordinal: int = 0


class EntityUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    aliases: Optional[list[str]] = None
    status: Optional[str] = None
    status_ordinal: Optional[int] = None


class IssueResolve(BaseModel):
    # 为什么忽略：同一条告警下次再出现时，人不用从头想一遍
    note: str = ""


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


@router.get("/studio/works/{work_id}/graph/timeline", dependencies=[Depends(verify_key)])
async def get_graph_timeline(work_id: str, request: Request):
    """图谱时间轴：按节把事件与人物状态排成一条线，供「时间轴」视图分组渲染。

    `events` 的 participants 与 `states` 的 name 都换成实体名字：前端只负责摆位置，
    不该为了一个 id 再拉一次图谱。空图返回三个空数组——刚立完项就点开时间轴
    不是错误。`state_json` 坏掉也不炸，退化成空对象。
    """
    store = _store(request)
    work = await _require_work(request, work_id)
    if (work.get("kind") or "") != "novel":
        raise HTTPException(status_code=400, detail="时间轴只用于小说；单篇文档看事实卡与术语表")

    sections = [
        {"id": n["id"], "ordinal": int(n["ordinal"]), "title": n["title"]}
        for n in await store.list_readable(work_id)
    ]
    names = {e["id"]: e["name"] for e in await store.list_entities(work_id)}

    events = []
    for ev in await store.list_events(work_id):
        try:
            pids = json.loads(ev.get("participants") or "[]")
        except json.JSONDecodeError:
            pids = []
        if not isinstance(pids, list):
            pids = []
        events.append({
            "ordinal": int(ev["ordinal"]),
            "summary": ev.get("summary") or "",
            "participants": [names[str(p)] for p in pids if str(p) in names],
            "story_time": ev.get("story_time") or "",
            "importance": int(ev.get("importance") or 1),
        })

    states = []
    for st in await store.list_states(work_id):
        try:
            state = json.loads(st.get("state_json") or "{}")
        except json.JSONDecodeError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        states.append({
            "entity_id": st["entity_id"],
            "name": names.get(st["entity_id"], ""),
            "ordinal": int(st["ordinal"]),
            "state": state,
        })
    return {"sections": sections, "events": events, "states": states}


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


@router.patch("/studio/foreshadows/{foreshadow_id}", dependencies=[Depends(verify_key)])
async def update_foreshadow(
    foreshadow_id: str, body: ForeshadowUpdate, request: Request,
):
    """作者手动回收 / 弃用伏笔：抽出来的不一定都算数。"""
    store = _store(request)
    existing = await store.get_foreshadow(foreshadow_id)
    if not existing:
        raise HTTPException(status_code=404, detail="伏笔不存在")
    ordinal = int(body.ordinal or existing.get("planted_ordinal") or 0)
    try:
        row = await store.update_foreshadow(
            foreshadow_id, status=body.status, ordinal=ordinal)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    graph.invalidate(existing["work_id"])
    return row


@router.patch("/studio/entities/{entity_id}", dependencies=[Depends(verify_key)])
async def update_entity(entity_id: str, body: EntityUpdate, request: Request):
    """作者直接改图谱实体：改名 / 补别名 / 改类型 / 改状态。

    抽取器只做保守对齐，作者知道「阿昭就是林昭」；不给这条路，
    每次重抽都会把同一个人再拆一个节点出来。
    """
    store = _store(request)
    existing = await store.get_entity(entity_id)
    if not existing:
        raise HTTPException(status_code=404, detail="实体不存在")
    kind = body.kind
    if kind is not None and kind not in graph.ENTITY_KINDS:
        raise HTTPException(status_code=400, detail=f"实体类型不认：{kind}")
    status = body.status
    if status is not None and status not in graph.STATUSES:
        raise HTTPException(status_code=400, detail=f"实体状态不认：{status}")
    canonical = graph.canon(body.name) if body.name else None
    try:
        row = await store.update_entity(
            entity_id, name=body.name, kind=kind, canonical=canonical,
            aliases=body.aliases, status=status, status_ordinal=body.status_ordinal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    graph.invalidate(existing["work_id"])
    return row


@router.post("/studio/works/{work_id}/check", dependencies=[Depends(verify_key)])
async def run_check(work_id: str, body: CheckRequest, request: Request):
    """跑一致性检查。全部是本地结构化比对，不调模型。"""
    store = _store(request)
    work = await _require_work(request, work_id)
    resolved = content_types.resolve(
        work.get("kind") or "article", work.get("scale") or "", work.get("subtype") or "",
        work.get("channel") or "")
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
async def resolve_issue(issue_id: str, request: Request, body: IssueResolve | None = None):
    note = (body.note if body else "").strip()[:500]
    ok = await _store(request).resolve_issue(issue_id, note)
    if not ok:
        raise HTTPException(status_code=404, detail="告警不存在")
    return {"ok": True, "note": note}


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
    # 与 pipeline 同一口径：预估超预算默认拦下；闸门覆盖所有会调模型的任务类型
    ignore_budget: bool = False


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

    # 预算闸门：凡是要调模型的任务类型都估都拦——beats/polish/extract 这些
    # 单笔比写正文小，但走的也是计钱通道，积起来一样烧预算（审查 m14）。
    # check 是纯本地规则不花钱，不在名单里。
    if body.task_type in ("draft", "article_draft", "beats", "polish", "extract", "summarize"):
        model = pick_model(request.app.state.registry, body.model or None)
        est = await cost.estimate_pipeline(
            store, request.app.state.registry, work_id=work_id,
            sections=len(nodes), model=model,
            spent_before=await _spent_on_model(request, store, work_id, model),
        )
        if est["over_budget"] and not body.ignore_budget:
            raise _budget_402(est)

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
async def list_jobs(request: Request, work_id: str = "", status: str = "", limit: int = 50,
                   newest: bool = False):
    rows = await _store(request).list_jobs(work_id=work_id, status=status, limit=limit,
                                          newest=newest)
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


@router.get("/studio/feedback", dependencies=[Depends(verify_key)])
async def quality_feedback(request: Request, sample: int = 300):
    """质量反馈环：AI 生成的采纳率与采用后的存活率（全局，含所有作品）。

    只读聚合，直接从 runs / node_versions 现算，不新增表；口径细节与
    排除项都写在 store.feedback 的文档里。控制台「数据」页一次刷新打一趟，
    sample 限制存活率逐条比对的成本上限。
    """
    return await _store(request).feedback(sample=sample)

