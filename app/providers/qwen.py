"""通义千问适配器。

双路径：
- HTTP 快路径（需设环境变量 W2A_QWEN_HTTP=1 且已 recon 出 fixtures/qwen.json、缓存到 login_tongyi_ticket）：
  直接用 ticket 作 Bearer，按真实模板打千问后端 SSE，速度快、无需常驻浏览器。
- 浏览器捕获路径（默认，最稳）：与豆包/DeepSeek 一致，在真实页面驱动发送并旁路 tee 响应流。

ticket 约 1 个月有效，是三家中最能"长期免登录"的。
"""
from __future__ import annotations

import json
import os
from typing import AsyncIterator, Optional

import httpx
from loguru import logger

from app.api.schemas import ChatCompletionRequest
from app.core.errors import AuthExpired, ProviderError
from app.core.sse import DeltaEvent
from app.providers._browser_base import BrowserStreamProvider
from app.providers._replay import (
    SSEBuffer,
    fill_body,
    inject_headers,
    is_done,
    load_fixture,
    render_prompt,
    generic_extract_text,
)


class QwenAdapter(BrowserStreamProvider):
    def url_hint(self) -> str:
        return "qianwen"

    def input_selectors(self) -> list[str]:
        return ["div[contenteditable='true']", "[role='textbox']", "textarea"]

    def send_selectors(self) -> list[str]:
        return [
            "button[aria-label*='\u53d1\u9001']",
            "button[class*='send']",
            "div[class*='send-btn']",
            "[data-testid*='send']",
        ]

    def parse_payload(self, payload: str) -> Optional[DeltaEvent]:
        obj = json.loads(payload)
        # 千问格式: {"error_msg":"", "data": {"messages": [{"mime_type":"text/plain", "content":"..."}]}}
        data = obj.get("data")
        if isinstance(data, dict):
            messages = data.get("messages")
            if isinstance(messages, list):
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    mime = msg.get("mime_type", "")
                    # 跳过信号/路由类消息
                    if "signal" in mime or "meta" in mime:
                        continue
                    content = msg.get("content") or msg.get("text")
                    if content:
                        status = msg.get("status", "")
                        finish = "stop" if status == "complete" else None
                        return DeltaEvent(content=str(content), finish_reason=finish)
        # OpenAI 风格兆底
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0] or {}
            delta = c0.get("delta") or {}
            if delta.get("content"):
                return DeltaEvent(content=delta["content"], finish_reason=c0.get("finish_reason"))
        text = generic_extract_text(obj)
        if not text:
            return None
        return DeltaEvent(content=text)

    async def stream(self, req: ChatCompletionRequest) -> AsyncIterator[DeltaEvent]:
        if os.getenv("W2A_QWEN_HTTP", "0") == "1":
            try:
                async for ev in self._stream_http(req):
                    yield ev
                return
            except AuthExpired:
                raise
            except Exception as exc:  # noqa: BLE001 - HTTP 快路径失败则回落浏览器路径
                logger.warning(f"[qwen] HTTP 快路径失败，回落浏览器路径: {exc}")
        async for ev in super().stream(req):
            yield ev

    async def _stream_http(self, req: ChatCompletionRequest) -> AsyncIterator[DeltaEvent]:
        fixture = load_fixture("qwen")
        if not fixture or not fixture.get("endpoint") or fixture.get("body_template") is None:
            raise ProviderError("qwen", "HTTP 快路径需要 fixtures/qwen.json（请先 python -m scripts.recon qwen）", status=501)

        ticket = await self._get_ticket()
        if not ticket:
            raise AuthExpired("qwen")

        headers = inject_headers(fixture.get("headers", {}), {"authorization": f"Bearer {ticket}"})
        headers.setdefault("content-type", "application/json")
        body = fill_body(fixture["body_template"], render_prompt(req))

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            async with client.stream("POST", fixture["endpoint"], headers=headers, json=body) as resp:
                if resp.status_code in (401, 403):
                    raise AuthExpired("qwen")
                if resp.status_code >= 400:
                    raise ProviderError("qwen", f"上游返回 {resp.status_code}", status=502)
                sse = SSEBuffer()
                async for chunk in resp.aiter_text():
                    for payload in sse.feed(chunk):
                        if is_done(payload):
                            yield DeltaEvent(finish_reason="stop")
                            return
                        ev = self.parse_payload(payload)
                        if ev:
                            yield ev
                for payload in sse.flush():
                    ev = self.parse_payload(payload)
                    if ev:
                        yield ev
                yield DeltaEvent(finish_reason="stop")

    async def _get_ticket(self) -> Optional[str]:
        cred = self.ctx.sessions.load_cred("qwen")
        if cred and cred.get("cookie_value") and "ticket" in str(cred.get("cookie_name", "")):
            return cred["cookie_value"]
        # 回退：从持久化浏览器 context 读取
        try:
            hit = await self.ctx.sessions.find_cookie("qwen")
            return hit[1] if hit else None
        except Exception:  # noqa: BLE001
            return None
