"""OpenAI 兼容路由：/v1/chat/completions、/v1/models、/v1/models/refresh。"""
from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from app.api.schemas import (
    ChatCompletionRequest,
    ModelList,
    gen_id,
)
from app.core.errors import W2AError
from app.core.registry import Registry
from app.core.sse import collect, make_chunk

router = APIRouter()


def _registry(request: Request) -> Registry:
    return request.app.state.registry


async def verify_key(request: Request) -> None:
    """可选访问口令：仅当配置了 W2A_API_KEY 时启用。"""
    expected = getattr(request.app.state.settings, "api_key", None)
    if not expected:
        return
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else auth
    if token != expected:
        raise HTTPException(status_code=401, detail="无效的 API Key")


def _sse(payload: str) -> str:
    return f"data: {payload}\n\n"


@router.get("/v1/models", response_model=ModelList, dependencies=[Depends(verify_key)])
async def list_models(request: Request):
    reg = _registry(request)
    return ModelList(data=reg.all_models())


@router.get("/v1/capabilities", dependencies=[Depends(verify_key)])
async def capabilities(request: Request):
    """模型/模式能力矩阵：前端据此渲染模型分组与思考/联网/专家开关。"""
    reg = _registry(request)
    return {"providers": [a.capability_payload() for a in reg.adapters.values()]}


@router.post("/v1/models/refresh", dependencies=[Depends(verify_key)])
async def refresh_models(request: Request):
    reg = _registry(request)
    return {"data": [m.model_dump() for m in reg.all_models()]}


@router.post("/v1/chat/completions", dependencies=[Depends(verify_key)])
async def chat_completions(req: ChatCompletionRequest, request: Request):
    reg = _registry(request)
    adapter = reg.route(req.model)
    throttle = reg.throttle(adapter.key)
    store = request.app.state.store

    # 限流（每分钟）+ 会话预校验，失败以标准 HTTP 错误返回，不进入流
    throttle.check_rate()
    # 会话 id 必须真实存在，否则消息会写成孤儿；放在 ensure_session 前，避免白等一次浏览器启动
    if req.conversation_id and req.messages and await store.get_conversation(req.conversation_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {req.conversation_id}")
    await adapter.ensure_session()
    # 最小请求间隔（从上一次回复结束算起）：降低上游风控触发率
    waited = await throttle.wait_turn()
    if waited >= 0.5:
        logger.info(f"[{adapter.key}] 最小间隔限速，已等待 {waited:.1f}s")

    chat_id = gen_id()

    # 持久化用户消息
    if req.conversation_id and req.messages:
        last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
        if last_user:
            await store.add_message(req.conversation_id, "user", str(last_user))
            await store.auto_title(req.conversation_id, str(last_user))

    if req.stream:
        return StreamingResponse(
            _stream_with_persist(reg, adapter, throttle, req, chat_id, store),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async with throttle:
        try:
            resp = await collect(adapter.stream(req), req.model)
        except W2AError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"[{adapter.key}] 对话失败: {exc}") from exc
    # 持久化助手回复
    if req.conversation_id:
        content = resp.choices[0].message.content or ""
        await store.add_message(req.conversation_id, "assistant", content)
    return resp


async def _stream_with_persist(reg, adapter, throttle, req, chat_id, store) -> AsyncIterator[str]:
    """Streaming with message persistence."""
    accumulated_content = []
    first = True
    try:
        async with throttle:
            async for ev in adapter.stream(req):
                chunk = make_chunk(chat_id, req.model, ev, first=first)
                first = False
                if ev.content:
                    accumulated_content.append(ev.content)
                yield _sse(chunk.sse_data())
        yield "data: [DONE]\n\n"
    except Exception as exc:  # noqa: BLE001
        # 保留内部错误类型（如 content_filter/risk_control），否则客户端只能看到笼统的 stream_error
        if isinstance(exc, W2AError):
            err = {"error": {"message": exc.message, "type": exc.type, "code": exc.code}}
        else:
            err = {"error": {"message": str(exc), "type": "stream_error"}}
        yield _sse(json.dumps(err, ensure_ascii=False))
        yield "data: [DONE]\n\n"
    finally:
        # Persist assistant message
        if req.conversation_id and accumulated_content:
            full_text = "".join(accumulated_content)
            try:
                await store.add_message(req.conversation_id, "assistant", full_text)
            except Exception:  # noqa: BLE001
                pass
