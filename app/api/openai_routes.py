"""OpenAI 兼容路由：/v1/chat/completions、/v1/models。

流式链路在这里统一收口：断连取消、心跳、stop/max_tokens 软截断、思考内容落库。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from loguru import logger

from app.api.schemas import ChatCompletionRequest, ModelList, Usage, gen_id
from app.core.auth import verify_key
from app.core.cut import StreamCut, cut_text, normalize_stops
from app.core.errors import InvalidRequest, W2AError
from app.core.registry import Registry
from app.core.sse import DeltaEvent, collect, make_chunk

router = APIRouter()

# 思考阶段可能长时间没有增量：定期发 SSE 注释行，避免 nginx/反代判定超时
HEARTBEAT_SEC = max(0.05, float(os.getenv("W2A_HEARTBEAT_SEC", "15")))
# 客户端断连轮询间隔
DISCONNECT_POLL_SEC = 0.2


def _registry(request: Request) -> Registry:
    return request.app.state.registry


def _reject_unsupported(req: ChatCompletionRequest) -> None:
    """不支持的多候选必须显式报错：静默忽略会让客户端拿到“第一条”还以为 n 生效了。"""
    if req.n and req.n > 1:
        raise InvalidRequest(
            f"本服务不支持 n={req.n}（每次请求只生成一条回答）",
            code="unsupported_parameter",
        )


def _warn_headers(req: ChatCompletionRequest) -> dict[str, str]:
    """tools 等暂不支持的参数：不报错，但要给调用方可发现的信号。"""
    if req.tools or req.tool_choice:
        logger.warning(f"[{req.model}] 收到 tools/tool_choice，但本服务尚不支持工具调用，已忽略")
        return {"X-W2A-Warning": "tools-not-supported"}
    return {}


def _estimate_usage(text: str, reasoning: Optional[str]) -> Usage:
    """按 4 字符 ≈ 1 token 粗估（与 sse.collect 里的口径一致）。"""
    completion = int((len(text) + len(reasoning or "")) / 4)
    return Usage(prompt_tokens=0, completion_tokens=completion, total_tokens=completion)


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
async def chat_completions(req: ChatCompletionRequest, request: Request, response: Response):
    reg = _registry(request)
    _reject_unsupported(req)
    warn_headers = _warn_headers(req)
    adapter = reg.route(req.model)
    throttle = reg.throttle(adapter.key)
    store = request.app.state.store

    # 限流（每分钟）+ 会话预校验，失败以标准 HTTP 错误返回，不进入流
    throttle.check_rate()
    # 会话 id 必须真实存在，否则消息会写成孤儿；放在 ensure_session 前，避免白等一次浏览器启动
    if req.conversation_id and req.messages and await store.get_conversation(req.conversation_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {req.conversation_id}")
    # 重新生成：先丢掉上次的助手回复，避免刷新/导出时同一问题挂两段回答
    if req.conversation_id and req.regenerate:
        dropped = await store.delete_trailing_assistant(req.conversation_id)
        if dropped:
            logger.info(f"[{adapter.key}] 重新生成：已丢弃上一次的 {dropped} 条助手回复")
    await adapter.ensure_session()
    # 最小请求间隔（从上一次回复结束算起）：降低上游风控触发率
    waited = await throttle.wait_turn()
    if waited >= 0.5:
        logger.info(f"[{adapter.key}] 最小间隔限速，已等待 {waited:.1f}s")

    chat_id = gen_id()

    # 持久化用户消息（返回 id 给前端：编辑重发时要按 id 把旧消息及其后的回答一起删掉）
    user_msg_id = ""
    if req.conversation_id and req.messages:
        last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
        if last_user:
            await store.auto_title(req.conversation_id, str(last_user))
            # 重新生成时前端会把同一条用户消息原样再发一遍：末尾已经是它就不再写一条，
            # 否则刷新后同一个问题会出现两次（普通请求连发同一句话仍照常各记一条）
            prev = await store.last_message(req.conversation_id)
            same = bool(
                prev
                and prev.get("role") == "user"
                and str(prev.get("content") or "").strip() == str(last_user).strip()
            )
            if not (req.regenerate and same):
                saved = await store.add_message(req.conversation_id, "user", str(last_user))
                user_msg_id = saved.get("id") or ""

    extra_headers = {**warn_headers}
    if user_msg_id:
        extra_headers["X-W2A-User-Message-Id"] = user_msg_id

    if req.stream:
        return StreamingResponse(
            _stream_with_persist(reg, adapter, throttle, req, chat_id, store, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", **extra_headers},
        )

    async with throttle:
        try:
            resp = await collect(adapter.stream(req), req.model)
        except W2AError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"[{adapter.key}] 对话失败: {exc}") from exc
    msg = resp.choices[0].message
    text, why = cut_text(msg.content or "", normalize_stops(req.stop), req.max_tokens)
    if why:
        logger.info(f"[{adapter.key}] 非流式输出已按 {why} 截断")
    msg.content = text
    if why == "max_tokens":
        resp.choices[0].finish_reason = "length"
    resp.usage = _estimate_usage(text, msg.reasoning_content)
    # 持久化助手回复（含思考过程，刷新后仍能看到）
    if req.conversation_id:
        await store.add_message(
            req.conversation_id,
            "assistant",
            text,
            reasoning_content=msg.reasoning_content or "",
        )
    for key, value in extra_headers.items():
        response.headers[key] = value
    return resp


class _StreamPump:
    """把「等上游增量」与「心跳/断连检查」解耦。

    关键：上游 `__anext__()` 只能有一个在飞，且**不能因心跳超时被取消**——
    取消会把 CancelledError 抛进 async generator，直接把它关掉。
    所以这里把 read 任务跨心跳保持挂起，只在 200ms 短切片上等并顺手查断连。
    """

    def __init__(self, agen: AsyncIterator[DeltaEvent], request: Request):
        self.agen = agen
        self.request = request
        self._read: Optional[asyncio.Future] = None

    async def next(self) -> tuple[str, Optional[DeltaEvent]]:
        """返回 (kind, event)，kind ∈ event / end / idle / gone。"""
        if self._read is None:
            self._read = asyncio.ensure_future(self.agen.__anext__())
        loop = asyncio.get_running_loop()
        idle_since = loop.time()
        while True:
            elapsed = loop.time() - idle_since
            wait = max(0.0, min(DISCONNECT_POLL_SEC, HEARTBEAT_SEC - elapsed))
            done, _ = await asyncio.wait({self._read}, timeout=wait)
            if done:
                task, self._read = self._read, None
                try:
                    return "event", task.result()
                except StopAsyncIteration:
                    return "end", None
            if await self.request.is_disconnected():
                return "gone", None
            if loop.time() - idle_since >= HEARTBEAT_SEC:
                return "idle", None

    async def close(self) -> None:
        """收尾：先取消在飞的 __anext__，再关生成器（避免 aclose 报 already running）。"""
        if self._read is not None:
            self._read.cancel()
            with contextlib.suppress(BaseException):
                await self._read
            self._read = None
        with contextlib.suppress(Exception):
            await self.agen.aclose()


async def _stream_with_persist(
    reg, adapter, throttle, req, chat_id, store, request: Request
) -> AsyncIterator[str]:
    """流式下发：断连取消、心跳、stop/max_tokens 截断，并保存正文与思考过程。"""
    cut = StreamCut(normalize_stops(req.stop), req.max_tokens)
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    first = True
    sent_finish = False
    interrupted = False
    error_text: Optional[str] = None
    pump = _StreamPump(adapter.stream(req), request)

    def chunk(ev: DeltaEvent) -> str:
        nonlocal first
        payload = make_chunk(chat_id, req.model, ev, first=first).sse_data()
        first = False
        return _sse(payload)

    try:
        async with throttle:
            while True:
                kind, ev = await pump.next()
                if kind == "gone":
                    interrupted = True
                    logger.info(f"[{adapter.key}] 客户端已断开，取消本次上游生成")
                    break
                if kind == "idle":
                    yield ": ping\n\n"
                    continue
                if kind == "end" or ev is None:
                    break
                if ev.reasoning_content:
                    reasoning_parts.append(ev.reasoning_content)
                    yield chunk(DeltaEvent(reasoning_content=ev.reasoning_content))
                if ev.content:
                    safe = cut.feed(ev.content)
                    if safe:
                        content_parts.append(safe)
                        yield chunk(DeltaEvent(content=safe))
                    if cut.done:
                        logger.info(f"[{adapter.key}] 流式输出已按 {cut.reason} 截断")
                        break
                if ev.finish_reason:
                    # 先把 StreamCut 扣住的尾巴补发完，再发 finish_reason，避免正文落在收尾之后
                    tail = cut.flush()
                    if tail:
                        content_parts.append(tail)
                        yield chunk(DeltaEvent(content=tail))
                    yield chunk(DeltaEvent(finish_reason=ev.finish_reason))
                    sent_finish = True
                    break

        if not interrupted:
            # 上游没给 finish_reason 就结束时（如空闲超时），补一条收尾
            tail = cut.flush()
            if tail:
                content_parts.append(tail)
                yield chunk(DeltaEvent(content=tail))
            if cut.done and not sent_finish:
                yield chunk(DeltaEvent(finish_reason="length" if cut.reason == "max_tokens" else "stop"))
            elif not sent_finish:
                yield chunk(DeltaEvent(finish_reason="stop"))
            yield "data: [DONE]\n\n"
    except Exception as exc:  # noqa: BLE001
        # 保留内部错误类型（如 content_filter/risk_control），否则客户端只能看到笼统的 stream_error
        if isinstance(exc, W2AError):
            err = {"error": {"message": exc.message, "type": exc.type, "code": exc.code}}
            error_text = exc.message
        else:
            err = {"error": {"message": str(exc), "type": "stream_error"}}
            error_text = str(exc)
        yield _sse(json.dumps(err, ensure_ascii=False))
        yield "data: [DONE]\n\n"
    finally:
        # 无论正常结束、报错还是断连，都尽量把已生成的内容落库
        await pump.close()
        if req.conversation_id:
            text = "".join(content_parts)
            reasoning = "".join(reasoning_parts)
            if text:
                note = ""
                if error_text:
                    note = f"\n\n（生成中断：{error_text}）"
                elif interrupted:
                    note = "\n\n（客户端中断生成）"
                body = text + note
            elif error_text:
                body = f"⚠️ {error_text}"
            else:
                body = "（客户端中断生成）" if interrupted else ""
            if body or reasoning:
                try:
                    await store.add_message(
                        req.conversation_id, "assistant", body, reasoning_content=reasoning
                    )
                except Exception:  # noqa: BLE001 - 落库失败不能影响已下发的流
                    logger.debug("流式回复落库失败（忽略）", exc_info=True)
