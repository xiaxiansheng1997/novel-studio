"""浏览器流式 Provider 基类：统一"驱动页面发送 + 旁路捕获响应流 + 解析增量"的骨架。

各家差异收敛到三个可覆写点：
- url_hint()：要 tee 的响应 URL 关键字（默认识别 event-stream，兜底足够）。
- input_selectors()/send_selectors()：对话输入框与发送按钮选择器。
- parse_payload(payload) -> DeltaEvent | None：把一条 SSE data 载荷解析成归一增量。
"""
from __future__ import annotations

import time
from typing import AsyncIterator, Optional

from loguru import logger

from app.api.schemas import ChatCompletionRequest
from app.browser.session_manager import login_hint_on_page
from app.browser.stream_capture import (
    iter_raw_chunks,
    risk_cooldown_remaining,
    start_capture,
    trigger_send,
)
from app.config import get_settings
from app.core.base import ProviderAdapter
from app.core.control_tokens import strip_control_tokens
from app.core.errors import NeedsLogin, ProviderBlocked, W2AError
from app.core.sse import DeltaEvent
from app.providers._replay import SSEBuffer, is_done, last_user_text

# 流式空闲超时：超过该时间没有新块就结束，避免上游中断后长时间挂起
STREAM_IDLE_TIMEOUT = get_settings().stream_idle_timeout


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
        # 统一剔除上游夹带的站点 UI 占位标记（如千问 [[inline_action_reply_1]]）
        async for ev in strip_control_tokens(self._stream_raw(req)):
            yield ev

    async def _stream_raw(self, req: ChatCompletionRequest) -> AsyncIterator[DeltaEvent]:
        # 风控冷却：刚触发过人机验证时直接快速失败，不碰浏览器，避免加深风控
        remain = risk_cooldown_remaining(self.key)
        if remain > 0:
            logger.warning(f"[{self.key}] 风控冷却中，拒绝请求（剩余 {remain}s）")
            yield DeltaEvent(
                content=(
                    f"⚠️ 上游风控冷却中（剩余 {remain}s）：刚触发过人机验证，"
                    "请先在 /live 页面完成滑块验证；冷却结束后会自动恢复"
                )
            )
            yield DeltaEvent(content=None, finish_reason="stop")
            return
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
        # 登录外观探测（仅日志）：一眼区分“页面是游客态”还是“请求被风控拦了”
        try:
            hint = await login_hint_on_page(page)
        except Exception:  # noqa: BLE001
            hint = None
        logger.info(f"[{self.key}] capture 页 url={page.url[:100]} 已登录外观={hint}")
        # 先激活捕获，再驱动发送（SPA 可能在 Enter 后立刻发请求，trigger_send 返回前响应就到了）
        state = getattr(page, "_w2a_state", None)
        if state:
            state["capturing"] = True
            # 供 stream_capture 在触发 RGV587 时自动过滑块后重发
            state["prompt"] = prompt
            state["input_selectors"] = self.input_selectors() or None
            state["send_selectors"] = self.send_selectors() or None
            state["retried"] = False
            state["sliding"] = False
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
        got_any = False
        started = time.perf_counter()
        first_ms: Optional[int] = None
        async for piece in iter_raw_chunks(queue, timeout=STREAM_IDLE_TIMEOUT):
            for payload in sse.feed(piece):
                if is_done(payload):
                    yield DeltaEvent(finish_reason="stop")
                    return
                ev = self._safe_parse(payload, emitted=got_any)
                if ev is not None:
                    if ev.content:
                        got_any = True
                        if first_ms is None:
                            first_ms = int((time.perf_counter() - started) * 1000)
                            logger.info(f"[{self.key}] 首字延迟 {first_ms}ms")
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
            ev = self._safe_parse(payload, emitted=got_any)
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
        if not got_any:
            # 空回复诊断：把页面当前可见文本打出来（验证码/限流提示等）
            try:
                txt = (await page.inner_text("body"))[-400:]
                logger.warning(f"[{self.key}] 未捕获到回复，页面文本片段: {txt}")
            except Exception:  # noqa: BLE001
                pass
        total_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            f"[{self.key}] 流结束 首字={first_ms if first_ms is not None else '-'}ms 总耗时={total_ms}ms"
        )
        yield DeltaEvent(finish_reason="stop")

    def _safe_parse(self, payload: str, *, emitted: bool = False) -> Optional[DeltaEvent]:
        try:
            return self.parse_payload(payload)
        except ProviderBlocked:
            # 已经下发过正文时不能再改判为错误（会截断正常回答），只记录并跳过
            if not emitted:
                raise
            logger.warning(f"[{self.key}] 流中途出现拒绝卡片，已忽略")
            return None
        except W2AError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条解析失败不应中断整个流
            logger.debug(f"[{self.key}] 跳过无法解析的载荷: {exc} :: {payload[:120]}")
            return None
