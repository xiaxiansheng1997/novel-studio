"""浏览器流式捕获：在真实登录的页面里驱动发送，并把对话响应回传 Python。

设计：
- 不自己实现 PoW（DeepSeek）/ X-Signature（豆包），而是让真实前端去生成请求。
- 优先走 CDP 真流式：Network.streamResourceContent + Network.dataReceived，
  逐块把 SSE 文本推入队列，首字不必等整段响应结束。
- CDP 不可用或 W2A_CDP_STREAM=0 时，回落到 Playwright 原生 page.on("response")
  整段读取；两条路径共用触发器与 RGV587 自动过滑块逻辑。
- 各家选择器/解析不同：本模块只做通用管道，具体在 provider 里实现。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

from loguru import logger

from app.browser.cdp_stream import CDP_STREAM, DONE, ERR_PREFIX, CdpSseTee
from app.browser.live_browser import auto_slide_page
from app.core.blocked import looks_blocked

# 完成/错误哨兵由 cdp_stream 统一定义，CDP 与整段读取两条路径共用

# 触发 RGV587 时是否尝试自动过滑块（默认开）
AUTO_SLIDE = os.getenv("W2A_AUTO_SLIDE", "1").lower() not in ("0", "false", "no")

# 冷启动预热：页面刚加载就发送时，前端安全 SDK（阿里 baxia 等）还没算好请求签名，
# 上游会把请求判成机器，直接回固定拒绝卡片（表现为“无法回答这个问题”）。
WARMUP_MS = int(os.getenv("W2A_WARMUP_MS", "2500"))

# 风控冷却：触发 RGV587 等验证后，该 provider 在此窗口内直接快速失败，
# 避免客户端自动重试在验证期间继续请求、不断加深风控。
RISK_COOLDOWN_SEC = float(os.getenv("W2A_RISK_COOLDOWN", "60"))
_risk_until: dict[str, float] = {}


def mark_risk(key: str, reason: str = "") -> None:
    """标记某个 provider 进入风控冷却期。"""
    _risk_until[key] = time.monotonic() + RISK_COOLDOWN_SEC
    logger.warning(f"[{key}] 进入风控冷却 {int(RISK_COOLDOWN_SEC)}s: {reason}")


def risk_cooldown_remaining(key: str) -> int:
    """返回剩余冷却秒数（0 表示未在冷却）。"""
    until = _risk_until.get(key)
    if not until:
        return 0
    remain = until - time.monotonic()
    if remain <= 0:
        _risk_until.pop(key, None)
        return 0
    return int(remain + 0.999)  # 向上取整


def clear_risk(key: str) -> None:
    """用户已完成人机验证时，手动清除冷却。"""
    if _risk_until.pop(key, None):
        logger.info(f"[{key}] 风控冷却已手动清除")


def _is_chat_path(url: str) -> bool:
    """判断 URL 路径是否像对话接口（而非会话列表/埋点等）。"""
    path = urlparse(url).path.lower()
    return any(seg in path for seg in ("/chat", "/conversation", "/completion"))


def _same_host(a: str, b: str) -> bool:
    """两个 URL 是否同主机（用于判断 capture 页能否直接复用）。"""
    try:
        ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    except ValueError:
        return False
    return bool(ha) and ha == hb


async def _warmup(page, key: str) -> None:
    """等 SPA 水合 + 安全 SDK 就绪，避免页面刚加载就发送被判机器。"""
    try:
        await page.wait_for_load_state("load", timeout=10000)
    except Exception:  # noqa: BLE001
        pass
    try:
        await page.wait_for_selector(
            "div[contenteditable='true'], textarea, [role='textbox']", timeout=15000
        )
    except Exception:  # noqa: BLE001
        logger.warning(f"[{key}] 预热等待输入框超时，仍尝试发送")
    if WARMUP_MS > 0:
        await page.wait_for_timeout(WARMUP_MS)


async def start_capture(pool, key: str, *, home_url: str, headless: bool, url_hint: str = ""):
    """准备一个带响应监听的页面，返回 (page, queue)。

    优先用 CDP 逐块读取 SSE；CDP 不可用时回落到 Playwright 整段读取。
    每次 start_capture 会重置监听状态，只捕获一个响应（对应一次 trigger_send）。
    """
    ctx = await pool.context(key, headless=headless)
    queue: asyncio.Queue = asyncio.Queue()
    # 两阶段：先不激活捕获，等 trigger_send 后由 _browser_base 设置 capturing=True
    state = {
        "capturing": False,
        "cdp": None,
        "matched_response": None,
        "fallback_started": False,
    }

    page = await pool.get_page(key, "capture")
    page._w2a_state = state  # 挂到 page 对象上供外部访问

    # 移除上一次的监听器（避免累积）—— 用 context 级别以覆盖 SPA/SW/iframe
    old_handler = getattr(ctx, "_w2a_resp_handler", None)
    if old_handler:
        try:
            ctx.remove_listener("response", old_handler)
        except Exception:  # noqa: BLE001
            pass

    async def _read_and_emit(response) -> None:
        """读取匹配的响应体并推入队列。"""
        try:
            body = await response.text()
            logger.info(f"[{key}] captured response url={response.url[:80]} status={response.status} body_len={len(body)}")
            if body:
                await queue.put(body)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[{key}] read response failed: {exc}")
            await queue.put(f"{ERR_PREFIX}{exc}")
        finally:
            await queue.put(DONE)

    async def _inspect_json(response) -> None:
        """检查对话路径上的非流式 JSON：是错误则快速失败，是预取响应则忽略。"""
        try:
            body = await response.text()
        except Exception:  # noqa: BLE001
            return
        low = body[:2000].lower()
        # 固定拒绝卡片可能以非流式 JSON 返回：它不是“预取”，必须显式报错
        if looks_blocked(body):
            state["capturing"] = False
            logger.error(
                f"[{key}] 上游返回固定拒绝卡片（非模型回答）: "
                f"url={response.url[:100]} body={body[:200]}"
            )
            await queue.put(
                f"{ERR_PREFIX}上游返回固定拒绝卡片（内容审核/风控/未登录游客态），不是模型回答；"
                f"请先在 /live 确认 /live/loggedin/{key} 返回 logged_in=true 后重试"
            )
            await queue.put(DONE)
            return
        is_err = response.status >= 400
        # 阿里风控人机验证（RGV587/FAIL_SYS_USER_VALIDATE）：先尝试自动过滑块并重发一次
        if "rgv587" in low or "fail_sys_user_validate" in low or "punish" in low:
            logger.error(f"[{key}] 触发上游人机验证(RGV587): {body[:200]}")
            # 同一请求可能产生多条含 RGV587 的响应（punish 重放/双发送）；
            # 滑动进行中的重复响应直接忽略，由正在跑的那条链路统一裁决
            if state.get("sliding"):
                logger.info(f"[{key}] 自动过滑块进行中，忽略重复的 RGV587 响应")
                return
            if AUTO_SLIDE and not state.get("retried") and state.get("prompt"):
                state["retried"] = True
                state["sliding"] = True
                state["capturing"] = False
                logger.info(f"[{key}] 尝试自动过滑块…")
                try:
                    res = await auto_slide_page(page, key=key, find_timeout=6.0, verify_timeout=5.0)
                except Exception as exc:  # noqa: BLE001
                    res = {"ok": False, "message": str(exc)}
                finally:
                    state["sliding"] = False
                if res.get("ok"):
                    logger.info(f"[{key}] 自动过滑块成功，重发消息: {res.get('message')}")
                    clear_risk(key)
                    state["capturing"] = True
                    try:
                        await trigger_send(
                            page,
                            state["prompt"],
                            input_selectors=state.get("input_selectors"),
                            send_selectors=state.get("send_selectors"),
                            wait_timeout=15.0,
                        )
                        return  # 已重发，继续等新的流式响应
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[{key}] 验证后重发失败: {exc}")
                        state["capturing"] = False
                else:
                    logger.warning(f"[{key}] 自动过滑块失败: {res.get('message')}")
            mark_risk(key, "触发 RGV587 人机验证")
            state["capturing"] = False
            await queue.put(
                f"{ERR_PREFIX}千问触发了人机验证(RGV587)，自动过滑块未成功，请打开 /live 页面手动完成验证后再重试"
                f"（{int(RISK_COOLDOWN_SEC)}s 风控冷却期内请求会被直接拒绝）"
            )
            await queue.put(DONE)
            return
        obj = None
        try:
            obj = json.loads(body)
        except Exception:  # noqa: BLE001
            pass
        if isinstance(obj, dict):
            if obj.get("success") is False:
                is_err = True
            if obj.get("errorCode") not in (None, "", 0, "0"):
                is_err = True
            if obj.get("errorMsg"):
                is_err = True
            ret = obj.get("ret")
            if isinstance(ret, list) and any(str(x).startswith("FAIL") for x in ret):
                is_err = True
            if obj.get("code") not in (None, 0, "0", 200, "200") and obj.get("message"):
                is_err = True
        if not is_err:
            logger.debug(f"[{key}] 忽略对话路径的非流式 JSON（疑似预取）: {body[:120]}")
            return
        state["capturing"] = False
        logger.error(f"[{key}] 对话接口未返回流式内容: status={response.status} body={body[:300]}")
        await queue.put(f"{ERR_PREFIX}上游对话接口返回错误（status={response.status}）: {body[:300]}")
        await queue.put(DONE)

    def _fallback_to_legacy(reason: str) -> None:
        """CDP 在拿到任何数据前失败时，交给 Playwright 整段读取兜底。"""
        if state.get("fallback_started"):
            return
        state["fallback_started"] = True
        state["cdp"] = None
        # 响应已完成时才调用 streamResourceContent 属正常情况，降为 debug
        benign = "already finished loading" in reason
        (logger.debug if benign else logger.warning)(f"[{key}] CDP 流式未接管，改走整段读取: {reason}")

        async def _wait_and_read() -> None:
            # Playwright 的 response 事件可能比 CDP 的失败回调稍晚到
            for _ in range(30):
                resp = state.get("matched_response")
                if resp is not None:
                    await _read_and_emit(resp)
                    return
                await asyncio.sleep(0.1)
            await queue.put(f"{ERR_PREFIX}CDP 流式捕获失败: {reason}")
            await queue.put(DONE)

        asyncio.ensure_future(_wait_and_read())

    def _on_response(response) -> None:
        if not state["capturing"]:
            return
        # 只捕 capture 页自己发出的请求（SW 请求 frame 为 None，放行）
        try:
            fr = response.frame
            if fr is not None and fr.page != page:
                return
        except Exception:  # noqa: BLE001
            pass
        url = response.url or ""
        ct = (response.headers.get("content-type") or "").lower()
        method = (response.request.method or "").upper()
        # 只捕 POST/PUT 的流式/目标响应
        if method not in ("POST", "PUT"):
            return
        is_stream = "event-stream" in ct or "stream" in ct
        hint_match = bool(url_hint) and url_hint.lower() in url.lower()
        # 诊断：记录所有命中域名的 POST 响应（含非流式错误响应）
        if hint_match and method in ("POST", "PUT"):
            logger.debug(f"[{key}] POST候选: status={response.status} ct={ct[:40]} url={url[:90]}")
            # 对话接口返回非流式 JSON：可能是错误也可能是预取，读出内容再判断
            if not is_stream and "json" in ct and _is_chat_path(url):
                asyncio.ensure_future(_inspect_json(response))
                return
        # 必须是流式响应，且（若无提示则任意流式、若有提示则 URL 需匹配）
        if is_stream and (not url_hint or hint_match):
            logger.info(f"[{key}] response matched: url={url[:80]} ct={ct[:40]} stream={is_stream} hint={hint_match}")
            state["capturing"] = False  # 一次只捕一个
            state["matched_response"] = response
            if state.get("cdp") is not None and not state.get("fallback_started"):
                logger.debug(f"[{key}] SSE 交由 CDP 逐块读取")
                return
            if not state.get("fallback_started"):
                asyncio.ensure_future(_read_and_emit(response))

    ctx._w2a_resp_handler = _on_response
    ctx.on("response", _on_response)

    # 导航到首页（SPA 加载）。
    # 关键：已在目标站点时直接复用页面，不要每次请求都重新 goto——
    # 重新加载后 1 秒内就发送，前端安全 SDK 还没就绪，上游会直接回固定拒绝卡片。
    final_url = getattr(page, "_w2a_final_url", "")
    reuse = (page.url or "").startswith("http") and (
        _same_host(page.url, home_url) or (bool(final_url) and _same_host(page.url, final_url))
    )
    if reuse:
        logger.info(f"[{key}] 复用已打开的 capture 页面，跳过导航: {page.url[:100]}")
    else:
        try:
            await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{key}] goto 首页异常（继续尝试）: {exc}")
        # 记录跳转后的真实地址（tongyi.aliyun.com/qianwen 会跳到 chat.qwen.ai），
        # 下次才能按真实主机判断是否可复用
        page._w2a_final_url = page.url
        await _warmup(page, key)
    logger.info(f"[{key}] capture page ready, url={page.url[:100]}")

    if CDP_STREAM:
        try:
            tee = await CdpSseTee.for_page(page)
            tee.arm(queue, url_hint, on_fallback=lambda reason: _fallback_to_legacy(reason))
            state["cdp"] = tee
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{key}] CDP 真流式不可用，使用整段读取: {exc}")
    else:
        logger.info(f"[{key}] W2A_CDP_STREAM=0，使用整段读取")

    return page, queue


async def iter_raw_chunks(queue: asyncio.Queue, *, timeout: float = 120.0) -> AsyncIterator[str]:
    """消费页面推回的原始文本块，直到 done 哨兵或超时。"""
    while True:
        try:
            piece = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("stream 捕获超时，结束本次读取")
            break
        if piece == DONE:
            break
        if isinstance(piece, str) and piece.startswith(ERR_PREFIX):
            raise RuntimeError(f"浏览器捕获流错误: {piece[len(ERR_PREFIX):]}")
        yield piece


async def trigger_send(
    page,
    prompt: str,
    *,
    input_selectors: Optional[list[str]] = None,
    send_selectors: Optional[list[str]] = None,
    wait_timeout: float = 25.0,
    clear_first: bool = False,
) -> None:
    """驱动真实页面发送：等待输入框渲染 -> 聚焦 -> 输入(兼容中文) -> 回车（或点发送按钮）。

    SPA 首屏的对话编辑器常有渲染延迟，goto 后立即查找会漏；故先轮询等待再驱动。
    输入用 keyboard.insert_text（等价 IME 上屏），对中文与 React 受控组件都比逐字更稳。
    clear_first：/live 辅助输入复用同一个输入框，先清掉上一轮残留文本再输入。
    """
    input_selectors = input_selectors or [
        "textarea",
        "div[contenteditable='true']",
        "[role='textbox']",
        "[contenteditable]:not([contenteditable='false'])",
    ]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + wait_timeout
    ta = None
    while loop.time() < deadline:
        for sel in input_selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    ta = loc
                    break
            except Exception:  # noqa: BLE001
                continue
        if ta is not None:
            break
        await page.wait_for_timeout(300)
    if ta is None:
        raise RuntimeError("未找到对话输入框：页面结构可能已变化或加载超时，请核对选择器或重跑 recon")

    await ta.click(timeout=8000)
    # SPA 输入框早于应用水合完成就可见，立即输入可能被框架状态重置导致“空提交”
    await page.wait_for_timeout(400)

    if clear_first:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(150)

    async def _editor_text() -> str:
        try:
            return await ta.evaluate("el => (el.value !== undefined ? el.value : el.innerText) || ''") or ""
        except Exception:  # noqa: BLE001
            return ""

    # 输入后回读校验，被清空则重试（hydration 完成后即可稳定）
    for attempt in range(3):
        try:
            await page.keyboard.insert_text(prompt)
        except Exception:  # noqa: BLE001
            await ta.press_sequentially(prompt, delay=15)
        await page.wait_for_timeout(150)
        current = await _editor_text()
        if prompt.strip() and prompt[:10] in current:
            break
        logger.debug(f"[trigger_send] 第{attempt + 1}次输入未生效(当前={current[:30]!r})，清空重试")
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(250)

    await page.keyboard.press("Enter")

    # 某些站点回车不发送（或只是换行），需点按钮：作为可选兜底
    for sel in (send_selectors or []):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_enabled():
                await btn.click()
                break
        except Exception:  # noqa: BLE001
            continue
