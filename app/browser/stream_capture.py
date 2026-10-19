"""浏览器流式捕获：在真实登录的页面里驱动发送，并把对话响应回传 Python。

设计（对应用户选择的 Playwright 路线）：
- 不自己实现 PoW（DeepSeek）/ X-Signature（豆包），而是让真实前端去生成请求，我们用
  Playwright 原生的 page.on("response") 事件来读取匹配的对话响应体。
- 比 add_init_script + expose_binding 的 JS hook 方案更可靠：不依赖页面执行环境、
  不受 Service Worker / Web Worker 干扰、不需要绑定注册。
- 各家选择器/解析不同：本模块只做通用管道，具体在 provider 里实现。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

from loguru import logger

# 完成/错误哨兵
DONE = "__W2A_DONE__"
ERR_PREFIX = "__W2A_ERR__"


async def start_capture(pool, key: str, *, home_url: str, headless: bool, url_hint: str = ""):
    """准备一个带响应监听的页面，返回 (page, queue)。

    使用 Playwright 的 page.on("response") 事件来捕获匹配的 SSE/流式响应。
    每次 start_capture 会重置监听状态，只捕获一个响应（对应一次 trigger_send）。
    """
    ctx = await pool.context(key, headless=headless)
    queue: asyncio.Queue = asyncio.Queue()
    # 两阶段：先不激活捕获，等 trigger_send 后由 _browser_base 设置 capturing=True
    state = {"capturing": False}

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

    def _on_response(response) -> None:
        if not state["capturing"]:
            return
        url = response.url or ""
        ct = (response.headers.get("content-type") or "").lower()
        method = (response.request.method or "").upper()
        # 只捕 POST/PUT 的流式/目标响应
        if method not in ("POST", "PUT"):
            return
        is_stream = "event-stream" in ct or "stream" in ct
        hint_match = bool(url_hint) and url_hint.lower() in url.lower()
        # 必须是流式响应，且（若无提示则任意流式、若有提示则 URL 需匹配）
        if is_stream and (not url_hint or hint_match):
            logger.info(f"[{key}] response matched: url={url[:80]} ct={ct[:40]} stream={is_stream} hint={hint_match}")
            state["capturing"] = False  # 一次只捕一个
            asyncio.ensure_future(_read_and_emit(response))

    ctx._w2a_resp_handler = _on_response
    ctx.on("response", _on_response)

    # 导航到首页（SPA 加载）
    try:
        await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[{key}] goto 首页异常（继续尝试）: {exc}")
    logger.info(f"[{key}] capture page ready, url={page.url[:100]}")

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
) -> None:
    """驱动真实页面发送：等待输入框渲染 -> 聚焦 -> 输入(兼容中文) -> 回车（或点发送按钮）。

    SPA 首屏的对话编辑器常有渲染延迟，goto 后立即查找会漏；故先轮询等待再驱动。
    输入用 keyboard.insert_text（等价 IME 上屏），对中文与 React 受控组件都比逐字更稳。
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

    await ta.click()
    await page.wait_for_timeout(80)
    # 用 insert_text 输入（中文/CJK 与受控组件更稳）；失败则回落逐字
    try:
        await page.keyboard.insert_text(prompt)
    except Exception:  # noqa: BLE001
        await ta.press_sequentially(prompt, delay=15)
    await page.wait_for_timeout(150)
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
