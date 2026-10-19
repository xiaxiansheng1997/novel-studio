"""内嵌实时浏览器：把服务器端真实页面用 CDP 截屏推送到网页，并把网页的输入回传给页面。

用途：在 Web UI 里直接完成"登录真实站点"与"录制对话接口"，无需命令行、无需外部窗口。
- 视频：Page.startScreencast -> screencastFrame(jpeg base64) -> 广播给所有订阅者(WebSocket)。
- 输入：网页鼠标/键盘事件 -> Input.dispatchMouseEvent / dispatchKeyEvent / insertText。
- 录制：可选挂 NetworkRecorder，停止时写出 fixtures/<key>.json。

并发/资源要点：
- 每个 LiveSession 自带锁，start/navigate/pause 串行化，避免并发重复 startScreencast / goto。
- 无订阅者时自动 stopScreencast（省 CPU），有新订阅再恢复；Page.enable 与帧监听只注册一次。
- 会话被关闭后 cdp 置 None；管理器据此判定"僵尸会话"并回收重建。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from app.browser.network_recorder import NetworkRecorder
from app.browser.playwright_pool import BrowserPool
from app.config import AppConfig

VIEWPORT_W = 1280
VIEWPORT_H = 800

# 常用功能键的 Windows 虚拟键码（CDP Input.dispatchKeyEvent 依赖它识别编辑类按键）
_VKEY = {
    "Backspace": 8, "Tab": 9, "Enter": 13, "Shift": 16, "Control": 17,
    "Alt": 18, "CapsLock": 20, "Escape": 27, "PageUp": 33, "PageDown": 34,
    "End": 35, "Home": 36, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39,
    "ArrowDown": 40, "Delete": 46, "F1": 112, "F5": 116, "F12": 123,
}


class LiveSession:
    def __init__(self, key: str, page, cdp):
        self.key = key
        self.page = page
        self.cdp = cdp
        self.recorder: Optional[NetworkRecorder] = None
        self.subscribers: set[asyncio.Queue] = set()
        self.started = False      # 截屏是否在推流
        self._enabled = False     # Page.enable + 帧监听是否已注册（只注册一次）
        self._dsf = 1             # deviceScaleFactor，用于前端坐标换算
        self._lock = asyncio.Lock()

    # ---- 生命周期 ----
    async def _ensure_enabled(self) -> None:
        if self._enabled or self.cdp is None:
            return
        await self.cdp.send("Page.enable")
        try:
            await self.cdp.send(
                "Emulation.setDeviceMetricsOverride",
                {"width": VIEWPORT_W, "height": VIEWPORT_H, "deviceScaleFactor": 1, "mobile": False},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[live:{self.key}] 视口覆盖失败，坐标可能按真实窗口偏移: {exc}")
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self._enabled = True

    async def _start_cast(self) -> None:
        if self.started or self.cdp is None:
            return
        await self.cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 60, "maxWidth": VIEWPORT_W, "maxHeight": VIEWPORT_H, "everyNthFrame": 1},
        )
        self.started = True

    async def start(self, url: str = "") -> None:
        async with self._lock:
            if self.cdp is None:
                raise RuntimeError("会话已关闭")
            await self._ensure_enabled()
            await self._start_cast()
        if url:
            await self.navigate(url)

    async def close(self) -> None:
        async with self._lock:
            if self.started and self.cdp is not None:
                try:
                    await self.cdp.send("Page.stopScreencast")
                except Exception:  # noqa: BLE001
                    pass
            self.started = False
            self._enabled = False
            if self.recorder:
                # 关闭前若正在录制且已抓到请求，先落盘，避免自动跳转丢数据
                try:
                    if self.recorder.entries:
                        self.recorder.write_fixture(self.key)
                except Exception:  # noqa: BLE001
                    pass
                self.recorder.detach()
                self.recorder = None
            if self.cdp is not None:
                try:
                    await self.cdp.detach()
                except Exception:  # noqa: BLE001
                    pass
                self.cdp = None

    # ---- 帧广播 ----
    async def _ack(self, sid) -> None:
        try:
            if self.cdp is not None:
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        except Exception:  # noqa: BLE001
            pass

    def _on_frame(self, params: dict) -> None:
        # 无论是否有订阅者都要 ack，否则 Chromium 会停止产帧
        try:
            asyncio.ensure_future(self._ack(params.get("sessionId")))
        except Exception:  # noqa: BLE001
            pass
        if not self.subscribers:
            return
        meta = params.get("metadata", {}) or {}
        self._dsf = meta.get("deviceScaleFactor", self._dsf) or 1
        msg = {
            "t": "frame",
            "data": params.get("data"),
            "w": meta.get("width", VIEWPORT_W),
            "h": meta.get("height", VIEWPORT_H),
            "dsf": self._dsf,
            "scroll": meta.get("scrollOffsetY", 0),
        }
        for q in list(self.subscribers):
            # latest-wins：先清空旧帧再放最新帧，慢客户端始终看到当前画面
            try:
                while not q.empty():
                    q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.subscribers.add(q)
        try:
            asyncio.ensure_future(self._resume_cast())
        except RuntimeError:
            pass  # 无运行中的事件循环（极少见）
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        if not self.subscribers:
            try:
                asyncio.ensure_future(self.pause_if_idle())
            except RuntimeError:
                pass

    async def _resume_cast(self) -> None:
        async with self._lock:
            if self.cdp is None:
                return
            await self._ensure_enabled()
            await self._start_cast()

    async def pause_if_idle(self) -> None:
        async with self._lock:
            if self.started and not self.subscribers and self.cdp is not None:
                try:
                    await self.cdp.send("Page.stopScreencast")
                except Exception:  # noqa: BLE001
                    pass
                self.started = False

    # ---- 输入回传 ----
    async def navigate(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[live:{self.key}] goto 失败: {exc}")

    async def reload(self) -> None:
        try:
            await self.page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass

    async def mouse(self, mtype: str, x: float, y: float,
                    button: Optional[str] = None, click_count: Optional[int] = None) -> None:
        if self.cdp is None:
            return
        params: dict = {"type": mtype, "x": x, "y": y}
        if mtype == "mouseMoved":
            # 移动：默认表示"未按住"（button=none, clickCount=0）；拖动时前端会显式带 left
            params["button"] = button or "none"
            params["clickCount"] = click_count if click_count is not None else 0
        else:
            params["button"] = button or "left"
            params["clickCount"] = click_count if click_count is not None else 1
        await self.cdp.send("Input.dispatchMouseEvent", params)

    async def wheel(self, x: float, y: float, dx: float, dy: float) -> None:
        if self.cdp is None:
            return
        await self.cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy},
        )

    async def press_key(self, etype: str, key: str, code: str, text: str = "", modifiers: int = 0) -> None:
        # 注意：不能命名为 key，否则与属性 self.key（provider 名）冲突
        if self.cdp is None:
            return
        params: dict = {"type": etype, "key": key, "code": code, "modifiers": modifiers}
        # 补上虚拟键码：Backspace/Delete/Enter/方向键等编辑键依赖它才能生效
        vk = _VKEY.get(key)
        if vk is None and key and len(key) == 1:
            vk = ord(key.upper())
        if vk is not None:
            params["windowsVirtualKeyCode"] = vk
            params["nativeVirtualKey"] = vk
        if etype == "keyDown":
            if text:
                params["text"] = text       # 可打印字符：keyDown 带 text 即产生输入
            else:
                params["type"] = "rawKeyDown"  # 功能键/组合键：不产生字符
        await self.cdp.send("Input.dispatchKeyEvent", params)

    async def insert_text(self, value: str) -> None:
        if self.cdp is None:
            return
        await self.cdp.send("Input.insertText", {"text": value})

    # ---- 录制 ----
    def record_start(self) -> None:
        if self.recorder:
            self.recorder.detach()  # 防止重复 attach 泄漏旧监听器
        self.recorder = NetworkRecorder()
        self.recorder.attach(self.page)

    def record_stop(self) -> Optional[dict]:
        if not self.recorder:
            return None
        res = self.recorder.write_fixture(self.key)
        self.recorder.detach()
        self.recorder = None
        return res


class LiveBrowserManager:
    def __init__(self, pool: BrowserPool, config: AppConfig):
        self.pool = pool
        self.config = config
        self.sessions: dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, key: str) -> LiveSession:
        async with self._lock:
            sess = self.sessions.get(key)
            if sess and sess.cdp is not None and not sess.page.is_closed():
                return sess
            if sess is not None:
                # 页面被手动关闭 / 会话已 detach：先彻底回收旧资源再重建
                try:
                    await sess.close()
                except Exception:  # noqa: BLE001
                    pass
            # 是否弹独立窗口由 provider 配置决定：千问等低门槛站可无头（不弹窗口），
            # DeepSeek/豆包需有头过 WAF/滑块（会弹真实窗口，属正常）。
            pcfg = self.config.providers.get(key)
            headless = bool(pcfg.headless) if pcfg else False
            ctx = await self.pool.context(key, headless=headless)
            page = await self.pool.get_page(key, "live")
            cdp = await ctx.new_cdp_session(page)
            sess = LiveSession(key, page, cdp)
            self.sessions[key] = sess
            return sess

    async def open(self, key: str, url: str = "") -> LiveSession:
        sess = await self.get_or_create(key)
        await sess.start(url)
        return sess

    async def close(self, key: str) -> None:
        async with self._lock:
            sess = self.sessions.pop(key, None)
        if sess:
            await sess.close()

    def get(self, key: str) -> Optional[LiveSession]:
        return self.sessions.get(key)

    async def close_all(self) -> None:
        for key in list(self.sessions.keys()):
            await self.close(key)
