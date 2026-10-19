"""浏览器流式 Provider 基类：统一"驱动页面发送 + 旁路捕获响应流 + 解析增量"的骨架。

各家差异收敛到三个可覆写点：
- url_hint()：要 tee 的响应 URL 关键字（默认识别 event-stream，兜底足够）。
- input_selectors()/send_selectors()：对话输入框与发送按钮选择器。
- parse_payload(payload) -> DeltaEvent | None：把一条 SSE data 载荷解析成归一增量。
"""
from __future__ import annotations

from typing import AsyncIterator, Optional

from loguru import logger

from app.api.schemas import ChatCompletionRequest
from app.browser.stream_capture import iter_raw_chunks, start_capture, trigger_send
from app.core.base import ProviderAdapter
from app.core.errors import NeedsLogin
from app.core.sse import DeltaEvent
from app.providers._replay import SSEBuffer, is_done, last_user_text


class BrowserStreamProvider(ProviderAdapter):
    # ---- 可覆写点 ----
    def url_hint(self) -> str:
        return ""

    def input_selectors(self) -> list[str]:
        return []  # 空则用 stream_capture 的通用兜底选择器

    def send_selectors(self) -> list[str]:
        return []

    def parse_payload(self, payload: str) -> Optional[DeltaEvent]:
        raise NotImplementedError

    def build_prompt(self, req: ChatCompletionRequest) -> str:
        # UI 输入框只适合发送"本轮用户消息"，上下文由站点自有会话携带
        return last_user_text(req)

    # ---- 会话校验 ----
    async def ensure_session(self) -> None:
        if not self.ctx.sessions.has_profile(self.key):
            raise NeedsLogin(self.key)

    # ---- 核心：浏览器捕获增量流 ----
    async def stream(self, req: ChatCompletionRequest) -> AsyncIterator[DeltaEvent]:
        await self.ensure_session()
        pcfg = self.cfg
        prompt = self.build_prompt(req)
        if not prompt:
            yield DeltaEvent(content="", finish_reason="stop")
            return

        sessions = self.ctx.sessions
        # 如果该 provider 的 live 会话还开着，先关闭它（避免双标签冲突导致 capture 发不出请求）
        live = self.ctx.live
        if live and live.get(self.key):
            logger.info(f"[{self.key}] 检测到 live 会话活跃，聊天前自动关闭")
            await live.close(self.key)
        page, queue = await start_capture(
            sessions.pool,
            self.key,
            home_url=pcfg.home_url,
            headless=pcfg.headless,
            url_hint=self.url_hint(),
        )
        # 先激活捕获，再驱动发送（SPA 可能在 Enter 后立刻发请求，trigger_send 返回前响应就到了）
        state = getattr(page, "_w2a_state", None)
        if state:
            state["capturing"] = True
        try:
            await trigger_send(
                page,
                prompt,
                input_selectors=self.input_selectors() or None,
                send_selectors=self.send_selectors() or None,
            )
            logger.info(f"[{self.key}] trigger_send OK, page_url={page.url[:80]}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{self.key}] 驱动发送失败: {exc}")
            # 把失败原因回传给前端，避免只看到一个空气泡
            yield DeltaEvent(content=f"⚠️ 驱动网页发送失败：{exc}")
            yield DeltaEvent(content=None, finish_reason="stop")
            return

        sse = SSEBuffer()
        prev_content = ""  # 跟踪累积式响应（千问等每次发全文的站点）
        async for piece in iter_raw_chunks(queue):
            for payload in sse.feed(piece):
                if is_done(payload):
                    yield DeltaEvent(finish_reason="stop")
                    return
                ev = self._safe_parse(payload)
                if ev is not None:
                    if ev.content:
                        # 累积式去重：若新内容以旧内容开头，只发新增部分
                        if ev.content.startswith(prev_content):
                            delta = ev.content[len(prev_content):]
                            prev_content = ev.content
                            if delta:
                                yield DeltaEvent(content=delta, finish_reason=ev.finish_reason)
                            elif ev.finish_reason:
                                yield DeltaEvent(content=None, finish_reason=ev.finish_reason)
                        else:
                            prev_content += ev.content
                            yield ev
                    else:
                        yield ev
        for payload in sse.flush():
            ev = self._safe_parse(payload)
            if ev is not None:
                if ev.content:
                    if ev.content.startswith(prev_content):
                        delta = ev.content[len(prev_content):]
                        if delta:
                            yield DeltaEvent(content=delta, finish_reason=ev.finish_reason)
                    else:
                        yield ev
                else:
                    yield ev
        yield DeltaEvent(finish_reason="stop")

    def _safe_parse(self, payload: str) -> Optional[DeltaEvent]:
        try:
            return self.parse_payload(payload)
        except Exception as exc:  # noqa: BLE001 - 单条解析失败不应中断整个流
            logger.debug(f"[{self.key}] 跳过无法解析的载荷: {exc} :: {payload[:120]}")
            return None
