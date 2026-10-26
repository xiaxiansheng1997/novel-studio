"""浏览器流式 Provider 基类：统一"驱动页面发送 + 旁路捕获响应流 + 解析增量"的骨架。

各家差异收敛到三个可覆写点：
- url_hint()：要 tee 的响应 URL 关键字（默认识别 event-stream，兜底足够）。
- input_selectors()/send_selectors()：对话输入框与发送按钮选择器。
- parse_payload(payload) -> DeltaEvent | None：把一条 SSE data 载荷解析成归一增量。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import AsyncIterator, Optional

from loguru import logger

from app.api.schemas import ChatCompletionRequest
from app.browser.live_browser import auto_slide_page, captcha_overlay_visible, detect_captcha
from app.browser.session_manager import login_hint_on_page
from app.browser.site_session import extract_chat_id, open_conversation
from app.browser.stream_capture import (
    AUTO_SLIDE,
    HUMAN_WAIT_SEC,
    RISK_COOLDOWN_SEC,
    clear_risk,
    iter_raw_chunks,
    mark_risk,
    risk_cooldown_remaining,
    start_capture,
    trigger_send,
)
from app.browser.ui_modes import apply_mode
from app.config import get_settings
from app.core.alerts import publish as publish_alert
from app.core.base import ProviderAdapter
from app.core.blocked import find_upstream_error, needs_human_verify
from app.core.control_tokens import strip_control_tokens
from app.core.errors import (
    HumanVerifyRequired,
    NeedsLogin,
    ProviderBlocked,
    ProviderError,
    UpstreamRejected,
    VerifyChallenge,
    W2AError,
)
from app.core.sse import DeltaEvent
from app.core.modes import resolve_mode
from app.providers._replay import SSEBuffer, is_done, last_user_text, render_prompt

# 流式空闲超时：超过该时间没有新块就结束，避免上游中断后长时间挂起
STREAM_IDLE_TIMEOUT = get_settings().stream_idle_timeout


def _drain_queue(queue: asyncio.Queue) -> None:
    """清空队列残留（重发前必须做，否则会把上一段流的结尾当成新流的结尾）。"""
    n = 0
    while True:
        try:
            queue.get_nowait()
            n += 1
        except Exception:  # noqa: BLE001 - QueueEmpty 也走这里
            break
    if n:
        logger.debug(f"已丢弃队列里 {n} 个残留块")


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

    def history_mode(self, req: ChatCompletionRequest) -> str:
        """多轮策略：请求级覆盖优先，其次 provider 配置，默认 site。"""
        mode = (req.history_mode or getattr(self.cfg, "history_mode", "site") or "site").lower()
        return "prompt" if mode == "prompt" else "site"

    def build_prompt(self, req: ChatCompletionRequest) -> str:
        # site 模式：只发本轮用户消息，上下文由站点自有会话携带
        # prompt 模式：把 system + 历史拼成一段发过去（每次请求都新开会话，见 app.browser.site_session）
        if self.history_mode(req) == "prompt":
            return render_prompt(req)
        return last_user_text(req)

    async def _remember_site_session(self, req: ChatCompletionRequest, page, *, allow: bool = True) -> None:
        """把当前页面 URL 里的站点会话 id 记到 sessions 表，供下次切换/复用。

        `allow=False` 用在会话切换失败时：页面可能还停在别的站点会话上，
        此时记录会把旧会话错配给当前会话（比不记录更糟，直接串台）。
        """
        if not allow or not req.conversation_id:
            return
        site_id = extract_chat_id(getattr(page, "url", "") or "")
        if not site_id:
            return
        try:
            if await self.resolve_internal_id(req) == site_id:
                return
            await self.save_internal_id(req, site_id)
            logger.info(f"[{self.key}] 记录站点会话 id={site_id}（API 会话 {req.conversation_id}）")
        except Exception as exc:  # noqa: BLE001 - 记录失败不影响对话本身
            logger.debug(f"[{self.key}] 记录站点会话 id 失败: {exc}")

    # ---- 会话校验 ----
    async def ensure_session(self) -> None:
        if not self.ctx.sessions.has_profile(self.key):
            publish_alert("login", self.key, "缺少有效登录态，需要到 /live 登录", level="error")
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
            publish_alert(
                "risk",
                self.key,
                f"上游风控冷却中（剩余 {remain}s），需先在 /live 完成人机验证",
                level="warn",
            )
            yield DeltaEvent(
                content=(
                    f"⚠️ 上游风控冷却中（剩余 {remain}s）：刚触发过人机验证，"
                    "请先在弹出的 Chrome 窗口（无头模式则 /live）里完成验证；冷却结束后会自动恢复"
                ),
                hint=True,
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
        # /live 与后台聊天共用同一个标签页：这里不能关闭 live 会话。
        # 关掉只会 detach CDP 并停推屏（页面还在），但 /live 画面会冻在最后一帧，
        # 用户就看不到随后弹出的验证码，也无法在画面里完成它。
        live = self.ctx.live
        if live and live.get(self.key):
            logger.info(f"[{self.key}] live 会话活跃：复用同一标签页，/live 画面可直接看到验证码与回复")
        page, queue = await start_capture(
            sessions.pool,
            self.key,
            home_url=pcfg.home_url,
            headless=pcfg.headless,
            url_hint=self.url_hint(),
        )
        # ---- 站点会话隔离：API 会话变了就切站点会话，避免上下文串台 ----
        prompt_mode = self.history_mode(req) == "prompt"
        site_id = await self.resolve_internal_id(req) if req.conversation_id else None
        current_site = extract_chat_id(page.url)
        need_switch = False
        site_switch_failed = False
        if prompt_mode:
            # prompt 模式自己拼历史：站点会话必须是干净的，否则历史会叠两遍
            need_switch = current_site is not None
        elif req.conversation_id:
            # 有映射就回到那条站点会话；没映射说明是新会话，页面停在旧会话上就新开
            need_switch = (current_site != site_id) if site_id else (current_site is not None)
        if need_switch:
            sw = await open_conversation(page, pcfg, None if prompt_mode else site_id)
            site_switch_failed = not sw["ok"]
            logger.info(
                f"[{self.key}] 站点会话切换 {(current_site or '(home)')} -> "
                f"{'(prompt-new)' if prompt_mode else (site_id or '(new)')}: {sw['action']} "
                f"ok={sw['ok']} {sw.get('error') or ''}".strip()
            )
            _drain_queue(queue)
        # 登录外观探测（仅日志）：一眼区分“页面是游客态”还是“请求被风控拦了”
        try:
            hint = await login_hint_on_page(page)
        except Exception:  # noqa: BLE001
            hint = None
        logger.info(f"[{self.key}] capture 页 url={page.url[:100]} 已登录外观={hint}")
        # 把页面切到请求要求的模型/模式（快速/专家、深度思考、联网）。
        # 失败只告警：模式没切过去也照样把问题发出去，比直接失败更有用。
        plan = resolve_mode(pcfg, req)
        self._mode_plan = plan
        url_before = page.url
        try:
            mode_status = await apply_mode(page, self.key, plan, pcfg)
        except Exception as exc:  # noqa: BLE001 - 模式对齐是增强，不应阻断发送
            logger.warning(f"[{self.key}] 模式对齐异常（继续发送）: {exc}")
            # 必须写一条失败状态：否则严格模式会读到上一条请求的 ok=True 而静默放行
            mode_status = {"ok": False, "missing": list(plan.changed_features), "preset": plan.preset_id, "error": str(exc)}
        self._last_mode_status = mode_status
        if os.getenv("W2A_STRICT_MODE", "0") == "1" and not mode_status.get("ok", True):
            raise ProviderError(self.key, f"模式未对齐：{mode_status.get('missing')}")
        try:
            # 换模型/模式在部分站点会新 tab 开会话，上游上下文从此重置
            if page.url != url_before:
                logger.warning(
                    f"[{self.key}] 模式切换后页面跳转 {url_before[:60]} -> {page.url[:60]}"
                    "（站点可能新开了会话，上游上下文会被重置）"
                )
        except Exception:  # noqa: BLE001 - 读 url 失败不影响发送
            pass
        # 点按钮可能触发站点自己的杂项请求，先清掉残留并重新武装捕获，
        # 否则可能把“切换模式”的响应当成这次对话的流。
        _drain_queue(queue)
        # 先激活捕获，再驱动发送（SPA 可能在 Enter 后立刻发请求，trigger_send 返回前响应就到了）
        state = getattr(page, "_w2a_state", None)
        if state:
            state["capturing"] = True
            # 供 stream_capture 在触发 RGV587 时自动过滑块后重发
            state["prompt"] = prompt
            # 重发前要用同一条请求的模式再对齐一次；挂在页面状态上而不是 self，
            # 并发（max_concurrency>1）时 self._mode_plan 会被另一条请求覆盖
            state["mode_plan"] = plan
            state["input_selectors"] = self.input_selectors() or None
            state["send_selectors"] = self.send_selectors() or None
            state["retried"] = False
            state["sliding"] = False
            rearm = state.get("rearm")
            if rearm:
                try:
                    rearm()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[{self.key}] 重新武装捕获失败: {exc}")
        try:
            await trigger_send(
                page,
                prompt,
                input_selectors=self.input_selectors() or None,
                send_selectors=self.send_selectors() or None,
            )
            logger.info(f"[{self.key}] trigger_send OK, page_url={page.url[:80]}")
            await self._remember_site_session(req, page, allow=not site_switch_failed)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{self.key}] 驱动发送失败: {exc}")
            hint = await self._send_failure_hint(exc, page)
            # 验证码“发送前”就盖住输入框（上一条被拒后留下的弹层）：
            # 有头浏览器里等人做完后重发一次；无头模式没人能操作，直接给指引
            recovered = False
            if AUTO_SLIDE and not getattr(pcfg, "headless", True):
                cap = await self._probe_captcha(page, timeout=2.0)
                if cap.get("kind") == "image" and await self._wait_human_verify(page, cap.get("text", "")):
                    resend_err = await self._resend_after_verify(page, queue, state, prompt)
                    recovered = not resend_err
                    if resend_err:
                        hint = f"{hint}（验证后重发失败：{resend_err}）"
                    else:
                        logger.info(f"[{self.key}] 验证完成后重发成功，继续等待回复")
            if not recovered:
                yield DeltaEvent(content=hint, hint=True)
                yield DeltaEvent(content=None, finish_reason="stop")
                return

        # 上游把“先去做人机验证”装在 200 的 SSE 里回时，自动处理验证 + 重发只给一次机会
        pending: Optional[VerifyChallenge] = None
        note = ""
        for attempt in range(2):
            try:
                async for ev in self._consume(page, queue):
                    yield ev
                # 发送后 SPA 才 pushState 到 /c/<id>，这里再记一次
                await self._remember_site_session(req, page, allow=not site_switch_failed)
                return
            except VerifyChallenge as vc:
                pending = vc
                if attempt == 0 and AUTO_SLIDE:
                    note = await self._solve_verify(page, queue, state, prompt, vc)
                    if not note:
                        continue  # 已过滑块并重发，接着消费新的流
                elif not AUTO_SLIDE:
                    note = "已关闭自动处理验证（W2A_AUTO_SLIDE=0）"
                else:
                    note = note or "自动处理验证后上游仍要求验证"
                break
        if pending is not None:
            mark_risk(self.key, note or "人机验证未通过")
            publish_alert(
                "captcha",
                self.key,
                f"自动处理人机验证未成功（{note or '未知原因'}），需到 /live 手动完成",
                level="error",
            )
            raise HumanVerifyRequired(
                self.key,
                pending.detail,
                note,
                headed=not pcfg.headless,
                cooldown=int(RISK_COOLDOWN_SEC),
                code=pending.code,
            )

    async def _consume(self, page, queue: asyncio.Queue) -> AsyncIterator[DeltaEvent]:
        """消费一次发送对应的响应流；上游要求人机验证时抛 VerifyChallenge。"""
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
                        if ev.reasoning_content:
                            got_any = True
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
        if not got_any:
            # 不能再默默返一条空回答：调用方拿到空 content 什么线索都没有
            raise ProviderError(
                self.key,
                "未捕获到任何回复内容：上游可能回了业务错误（未登录/限流/风控），"
                f"或页面结构与接口已变化。请打开 /live 手动发一句确认 {self.key} 能否正常回答",
            )
        yield DeltaEvent(finish_reason="stop")

    async def _probe_captcha(self, page, timeout: float = 4.0) -> dict:
        """验证弹层通常比错误事件晚半秒才渲染，短暂轮询一下再判题型。"""
        cap = {"kind": "none", "text": ""}
        deadline = time.monotonic() + timeout
        while True:
            try:
                cap = await detect_captcha(page)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{self.key}] 识别验证码题型失败: {exc}")
            if cap.get("kind") != "none" or time.monotonic() >= deadline:
                return cap
            await asyncio.sleep(0.5)

    async def _wait_human_verify(self, page, cap_text: str) -> bool:
        """图片点选类验证码只能人工做：有头浏览器里等人在窗口里完成，返回是否等到。"""
        wait = 0.0 if getattr(self.cfg, "headless", True) else HUMAN_WAIT_SEC
        if wait <= 0:
            return False
        logger.warning(
            f"[{self.key}] 这是图片点选验证码（{cap_text[:60]!r}），自动过滑块帮不上；"
            f"请在弹出来的 Chrome 窗口里勾选图片，最多等 {int(wait)}s…"
        )
        publish_alert(
            "captcha",
            self.key,
            f"检测到图片点选验证码（{cap_text[:40] or '按题面勾选图片'}），请在浏览器窗口里完成，最多等 {int(wait)}s",
            level="warn",
        )
        deadline = time.monotonic() + wait
        last_log = 0.0
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            try:
                kind = (await detect_captcha(page)).get("kind")
            except Exception:  # noqa: BLE001 - 页面跳转/刷新时忽略
                continue
            if kind == "none":
                logger.info(f"[{self.key}] 检测到验证码已消失（人工已完成），继续重发")
                return True
            elapsed = wait - max(deadline - time.monotonic(), 0.0)
            if elapsed - last_log >= 15:
                last_log = elapsed
                logger.info(f"[{self.key}] 仍在等待人工完成验证码…（已等 {int(elapsed)}s）")
        logger.warning(f"[{self.key}] 等满 {int(wait)}s 仍未检测到人工完成验证码")
        return False

    async def _resend_after_verify(self, page, queue: asyncio.Queue, state, prompt: str) -> str:
        """验证已通过：清冷却、清队列残留后重发同一条消息；返回空串表示已发出。"""
        clear_risk(self.key)
        # 上一段响应已经往队列里塞了残留块与 DONE，不清掉会让重发后的流立即结束
        _drain_queue(queue)
        # 页面可能刚刷新过（验证/跳转），模式会回到默认，重发前再对齐一次
        plan = (state or {}).get("mode_plan") or getattr(self, "_mode_plan", None)
        if plan is not None:
            try:
                self._last_mode_status = await apply_mode(page, self.key, plan, self.cfg)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{self.key}] 重发前模式对齐失败: {exc}")
        if state:
            state["capturing"] = True
            state["retried"] = True
            state["sliding"] = False
            # CDP tee 与 Playwright 监听都只捕一次，不重新武装的话重发会一直等到超时
            rearm = state.get("rearm")
            if rearm:
                try:
                    rearm()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[{self.key}] 重新启用响应捕获失败，重发可能收不到内容: {exc}")
        try:
            await trigger_send(
                page,
                prompt,
                input_selectors=self.input_selectors() or None,
                send_selectors=self.send_selectors() or None,
                wait_timeout=15.0,
                # 上一条失败后站点偶尔会把文本留在输入框，不清掉会变成发两遍
                clear_first=True,
            )
        except Exception as exc:  # noqa: BLE001
            return f"验证后重发失败：{exc}"
        return ""

    async def _solve_verify(self, page, queue: asyncio.Queue, state, prompt: str, vc) -> str:
        """自动处理一次验证并重发；成功返回空字符串，失败返回原因。

        分两条路：滑块类交给 auto_slide_page 自动拖；图片点选/拖拽类只能请人做，
        有头浏览器里等人做完再重发（W2A_HUMAN_WAIT_SEC，无头模式不等）。
        """
        _drain_queue(queue)
        cap = await self._probe_captcha(page)
        kind, cap_text = cap.get("kind", "none"), cap.get("text", "")
        logger.warning(
            f"[{self.key}] 上游要求人机验证（{vc.detail[:80] or vc.code}），"
            f"页面验证码类型={kind} 题面={cap_text[:60]!r}"
        )

        if kind == "image":
            # 图片点选/拖拽：要看着图做语义判断，机器代做不可靠，只能请人
            if await self._wait_human_verify(page, cap_text):
                return await self._resend_after_verify(page, queue, state, prompt)
            return (
                f"页面弹出了图片点选验证码（{cap_text[:50] or '按题面勾选图片并拖到下方'}），"
                "需要人工完成"
            )

        try:
            res = await auto_slide_page(page, key=self.key, find_timeout=6.0, verify_timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "message": str(exc)}
        note = res.get("message") or "自动过滑块未通过"
        if not res.get("ok"):
            if kind == "none" and not await captcha_overlay_visible(page):
                note += "；页面上也没有验证码容器，更像是纯限流而不是验证"
            return note
        logger.info(f"[{self.key}] 自动过滑块成功，重发消息：{note}")
        return await self._resend_after_verify(page, queue, state, prompt)

    # 验证码/风控弹窗会盖住输入框，Playwright 的超时日志对人毫无意义
    _CAPTCHA_HINTS = ("captcha", "verify", "滑块", "baxia", "punish", "nocaptcha")
    _DIALOG_HINTS = ("intercepts pointer events", "element is not enabled")

    async def _send_failure_hint(self, exc: Exception, page=None) -> str:  # noqa: ANN001 - page 可选
        text = str(exc).lower()
        if any(h in text for h in self._CAPTCHA_HINTS) or any(h in text for h in self._DIALOG_HINTS):
            # 先看页面上到底摆着什么：图片点选验证码不能关，营销弹窗可以关
            try:
                cap = await detect_captcha(page) if page is not None else None
            except Exception:  # noqa: BLE001
                cap = None
            if cap and cap.get("kind") == "image":
                return (
                    f"⚠️ {self.key} 页面上摆着图片点选验证码（{cap.get('text', '')[:40]}），盖住了输入框。"
                    "请先在浏览器窗口（无头模式则 /live）里勾选图片完成验证后重试"
                )
        if any(h in text for h in self._CAPTCHA_HINTS):
            return (
                f"⚠️ {self.key} 页面弹出了人机验证/验证码，盖住了输入框，无法代你发送。"
                "请在弹出的 Chrome 窗口（或 /live 画面，同一个标签页）里手动完成验证后重试"
            )
        if any(h in text for h in self._DIALOG_HINTS):
            # 实测豆包会弹“下载豆包电脑版”这类营销弹窗，它不是验证，但同样会挡住输入框
            return (
                f"⚠️ {self.key} 页面上有弹窗盖住了输入框（常见于“下载电脑版”这类引导弹窗），"
                "已尝试自动关闭但没成功。请在弹出的 Chrome 窗口里手动关掉弹窗后重试"
            )
        if page is not None:
            # 账号被禁言/封禁时站点会把输入框整个撤掉，页面只剩一行通知。
            # 这种情况重试多少次都没用，必须给出明确原因，让队列整队停下。
            try:
                body = await page.inner_text("body")
            except Exception:  # noqa: BLE001 - 读不到页面就按普通失败处理
                body = ""
            for marker in ("已被禁言", "账号已被封禁", "账号被封禁", "违反用户使用规范"):
                if marker in body:
                    tail = re.search(r"(?:禁言|封禁)至[^，。\n]{0,24}", body)
                    when = f"（{tail.group(0)}）" if tail else ""
                    return (
                        f"⚠️ {self.key} 账号受限{when}：站点已撤掉输入框，"
                        "重试不会恢复。请更换账号或等解封后再跑"
                    )
        if "未找到对话输入框" in text:
            return (
                f"⚠️ {self.key} 页面上没找到对话输入框：站点可能已改版或页面没加载完，"
                "请在 /live 确认页面状态后重试"
            )
        return f"⚠️ 驱动网页发送失败：{exc}"

    def _raise_upstream_error(self, payload: str, *, emitted: bool = False) -> None:
        """上游把限流/风控错误装在 200 的 SSE 里回时，必须显式报错，不能当成空回答。"""
        try:
            obj = json.loads(payload)
        except Exception:  # noqa: BLE001 - 非 JSON 载荷交给各家 parse_payload 处理
            return
        hit = find_upstream_error(obj)
        if not hit:
            return
        code, detail = hit
        if needs_human_verify(obj):
            if emitted:
                # 正文已经下发，不能再改判为错误（会截断正常回答），只记冷却
                logger.warning(f"[{self.key}] 流中途出现人机验证信号，正文已下发，仅记录冷却 code={code}")
                mark_risk(self.key, "流中途要求人机验证")
                return
            logger.warning(f"[{self.key}] 上游要求人机验证 code={code}: {detail[:160]}")
            raise VerifyChallenge(code, detail)
        logger.error(f"[{self.key}] 上游返回业务错误 code={code}: {detail[:160]}")
        raise UpstreamRejected(self.key, code, detail)

    def _safe_parse(self, payload: str, *, emitted: bool = False) -> Optional[DeltaEvent]:
        try:
            self._raise_upstream_error(payload, emitted=emitted)
            return self.parse_payload(payload)
        except ProviderBlocked:
            # 已经下发过正文时不能再改判为错误（会截断正常回答），只记录并跳过
            if not emitted:
                raise
            logger.warning(f"[{self.key}] 流中途出现拒绝卡片，已忽略")
            return None
        except VerifyChallenge:
            raise  # 交给上层尝试自动过滑块
        except W2AError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条解析失败不应中断整个流
            logger.debug(f"[{self.key}] 跳过无法解析的载荷: {exc} :: {payload[:120]}")
            return None
