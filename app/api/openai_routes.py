"""OpenAI 兼容路由：/v1/chat/completions、/v1/models、/v1/models/refresh。"""
from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

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


@router.post("/v1/models/refresh", dependencies=[Depends(verify_key)])
async def refresh_models(request: Request):
    reg = _registry(request)
    return {"data": [m.model_dump() for m in reg.all_models()]}


@router.post("/v1/chat/completions", dependencies=[Depends(verify_key)])
async def chat_completions(req: ChatCompletionRequest, request: Request):
    reg = _registry(request)
    adapter = reg.route(req.model)
    throttle = reg.throttle(adapter.key)

    # 限流（每分钟）+ 会话预校验，失败以标准 HTTP 错误返回，不进入流
    throttle.check_rate()
    await adapter.ensure_session()

    chat_id = gen_id()

    if req.stream:
        return StreamingResponse(
            _stream(reg, adapter, throttle, req, chat_id),
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
    return resp


async def _stream(reg: Registry, adapter, throttle, req: ChatCompletionRequest, chat_id: str) -> AsyncIterator[str]:
    first = True
    try:
        async with throttle:
            async for ev in adapter.stream(req):
                chunk = make_chunk(chat_id, req.model, ev, first=first)
                first = False
                yield _sse(chunk.sse_data())
        yield "data: [DONE]\n\n"
    except Exception as exc:  # noqa: BLE001 - 流已开始，无法改状态码，以 error 事件下发
        err = {"error": {"message": str(exc), "type": "stream_error"}}
        yield _sse(json.dumps(err, ensure_ascii=False))
        yield "data: [DONE]\n\n"
