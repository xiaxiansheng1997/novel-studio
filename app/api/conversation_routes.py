"""会话管理 CRUD 路由：/v1/conversations。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from app.core.auth import verify_key

router = APIRouter()


def _store(request: Request):
    return request.app.state.store


class CreateConversationRequest(BaseModel):
    model: str
    title: str = "新对话"
    provider: str = ""
    system_prompt: str = ""


class UpdateConversationRequest(BaseModel):
    title: Optional[str] = None
    system_prompt: Optional[str] = None


@router.get("/v1/conversations", dependencies=[Depends(verify_key)])
async def list_conversations(request: Request, limit: int = 100, offset: int = 0):
    store = _store(request)
    return await store.list_conversations(limit=limit, offset=offset)


@router.post("/v1/conversations", dependencies=[Depends(verify_key)])
async def create_conversation(body: CreateConversationRequest, request: Request):
    store = _store(request)
    # Infer provider from model prefix
    provider = body.provider
    if not provider:
        config = request.app.state.config
        for key, pcfg in config.providers.items():
            if body.model.startswith(pcfg.model_prefix or key + "-"):
                provider = key
                break
    return await store.create_conversation(
        model=body.model,
        title=body.title,
        provider=provider,
        system_prompt=body.system_prompt,
    )


@router.get("/v1/conversations/{conv_id}", dependencies=[Depends(verify_key)])
async def get_conversation(conv_id: str, request: Request):
    store = _store(request)
    conv = await store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv


@router.patch("/v1/conversations/{conv_id}", dependencies=[Depends(verify_key)])
async def update_conversation(conv_id: str, body: UpdateConversationRequest, request: Request):
    store = _store(request)
    ok = await store.update_conversation(conv_id, title=body.title, system_prompt=body.system_prompt)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或无更新")
    return {"ok": True}


@router.delete("/v1/conversations/{conv_id}", dependencies=[Depends(verify_key)])
async def delete_conversation(conv_id: str, request: Request):
    store = _store(request)
    await store.delete_conversation(conv_id)
    return {"ok": True}


@router.get("/v1/conversations/{conv_id}/messages", dependencies=[Depends(verify_key)])
async def get_messages(conv_id: str, request: Request, limit: int = 200):
    store = _store(request)
    return await store.get_messages(conv_id, limit=limit)


@router.post("/v1/conversations/{conv_id}/export", dependencies=[Depends(verify_key)])
async def export_conversation(conv_id: str, request: Request):
    """导出为 Markdown 格式。"""
    store = _store(request)
    conv = await store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = await store.get_messages(conv_id, limit=1000)

    lines = [f"# {conv['title']}\n"]
    lines.append(f"- 模型: {conv['model']}")
    lines.append(f"- Provider: {conv['provider']}")
    lines.append(f"- 创建时间: {__import__('datetime').datetime.fromtimestamp(conv['created_at']).strftime('%Y-%m-%d %H:%M')}\n")
    lines.append("---\n")
    for msg in messages:
        role_name = {"user": "你", "assistant": "助手", "system": "系统"}.get(msg["role"], msg["role"])
        lines.append(f"### {role_name}")
        if msg.get("reasoning_content"):
            lines.append(f"<details>\n<summary>思考过程</summary>\n\n{msg['reasoning_content']}\n\n</details>\n")
        lines.append(f"{msg['content']}\n")
    return {"format": "markdown", "content": "\n".join(lines)}
