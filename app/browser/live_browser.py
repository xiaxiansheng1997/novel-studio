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
import math
import random
import time
from typing import Optional

from loguru import logger

from app.browser.network_recorder import NetworkRecorder
from app.browser.playwright_pool import BrowserPool
from app.config import AppConfig

VIEWPORT_W = 1280
VIEWPORT_H = 800

# 验证码滑块手柄：强特征选择器（只会出现在验证码内），直接信任
_SLIDER_STRONG = (
    "#nc_1_n1z",
    ".nc_iconfont.btn_slide",
)
# 泛化命名：页面上的普通滑块控件也可能命中，必须再校验它处在验证码容器内
# （字节/豆包的验证控件类名带 hash，只能靠 captcha 容器 + 语义关键字去认）
_SLIDER_WEAK = (
    "[id*='n1z']",
    ".verify-move-block",
    ".slider-btn",
    "[class*='move-block']",
    "[class*='drag']",
    "[class*='knob']",
    "[class*='slider']",
)
# 验证码滑块/轨道的常见选择器（阿里 nc、腾讯、字节、通用命名），按优先级尝试
_SLIDER_SELECTORS = _SLIDER_STRONG + _SLIDER_WEAK
# 泛化滑块必须位于这些容器之一（阿里 nc / 腾讯 / 字节 / 通用 captcha）
_CAPTCHA_CONTAINER = (
    ".nc_scale,[id*='nocaptcha'],.baxia-dialog,[id*='punish'],"
    ".verify-bar-area,.slider-track,.verify-wrap,[class*='captcha'],[id*='captcha'],"
    "[class*='verify']"
)
_TRACK_SELECTORS = (
    ".nc_scale",
    "#nc_1_wrapper",
    "[id*='scale_text']",
    ".verify-bar-area",
    ".slider-track",
    "[class*='track']",
    "[class*='slider']",
)
# 验证失败后“点击刷新/重试”的控件（必须限定在验证码容器内，裸 [id*='refresh'] 会误点页面其他刷新按钮）
_REFRESH_SELECTORS = (
    "#nc_1_refresh",
    ".nc_iconfont.btn_refresh",
    ".baxia-dialog [id*='refresh']",
    "[id*='nocaptcha'] [id*='refresh']",
)
# 图片点选/拖拽类验证码（字节 secsdk，豆包实测题面：“体育运动常用的物品 /
# 请选择所有符合上述描述的图片，并拖拽到下方”）：要看着图做语义判断，机器代做不可靠，
# 只能请人在浏览器里完成。这里只负责认出来，别拿滑块那套去拖。
_IMAGE_CAPTCHA_MARKERS = (
    "请选择所有符合",
    "拖拽到下方",
    "拖拽到这里",
    "请选出",
    "依次点击",
    "点击选择",
    "请选择下方",
)
_SLIDER_CAPTCHA_MARKERS = ("拖动滑块", "拖动到最右边", "完成拼图", "按住左边滑块", "向右拖动")

# 读页面上可见弹层/验证码容器的文字与图片数，用来判断验证码题型
_OVERLAY_PROBE_JS = """
() => {
  const out = [];
  const seen = new Set();
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 80) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none'
      && parseFloat(st.opacity || '1') > 0.05;
  };
  const push = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    if (!vis(el)) return;
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    const n = el.querySelectorAll('img').length;
    if (t || n >= 4) out.push({ text: t.slice(0, 300), imgs: n });
  };
  for (const el of document.querySelectorAll(
      "[role='dialog'],[class*='captcha'],[id*='captcha'],[class*='verify'],[id*='verify'],"
      + "[class*='dialogMask'],[class*='Modal'],[class*='modal'],"
      + "iframe[src*='captcha'],iframe[src*='verify'],iframe[src*='secsdk']")) {
    push(el.closest("[role='dialog']") || el);
  }
  // 兜底：浮层里挤着 6 张以上大图，基本就是图片点选验证码
  const boxes = new Set();
  for (const img of document.querySelectorAll('img')) {
    const r = img.getBoundingClientRect();
    if (r.width < 60 || r.height < 60) continue;
    let p = img.parentElement;
    for (let i = 0; p && i < 5; i++, p = p.parentElement) {
      // 图片网格本身可能是 static，真正的浮层在更上层，所以不能 break，逐层收集
      if (p.querySelectorAll('img').length >= 6) boxes.add(p);
    }
  }
  for (const b of boxes) {
    const st = getComputedStyle(b);
    if (st.position === 'fixed' || st.position === 'absolute') push(b);
  }
  return out;
}
"""

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
        self._slide_task: Optional[asyncio.Task] = None  # 自动过滑块任务（防重复点击并发）

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

    async def start(self, url: str = "", *, force_navigate: bool = False) -> None:
        async with self._lock:
            if self.cdp is None:
                raise RuntimeError("会话已关闭")
            await self._ensure_enabled()
            await self._start_cast()
        # 与后台聊天共用标签页时，页面通常已停在站点上：这时再 goto 一次会
        # 把正在进行的对话/验证码冲掉。只有空白页或显式指定地址才导航。
        if url and (force_navigate or not (self.page.url or "").startswith("http")):
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
            # 移动：默认表示“未按住”（button=none, buttons=0）；
            # 拖动时前端会显式带 left，此时必须同时给 buttons=1，
            # 否则 CDP 不把它当作拖拽，滑块不会跟手。
            dragging = button == "left"
            params["button"] = button or "none"
            params["clickCount"] = click_count if click_count is not None else 0
            params["buttons"] = 1 if dragging else 0
        else:
            params["button"] = button or "left"
            params["clickCount"] = click_count if click_count is not None else 1
            params["buttons"] = 1 if mtype == "mousePressed" else 0
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

    # ---- 自动过滑块 ----
    async def auto_slide(self) -> dict:
        """自动拖动当前 live 页面的验证码滑块；返回 {ok, message}。"""
        if self.cdp is None:
            return {"ok": False, "message": "会话已关闭"}
        cap = await detect_captcha(self.page)
        if cap["kind"] == "image":
            # 图片点选/拖拽：机器代做不可靠，直接告诉人要手动做
            return {
                "ok": False,
                "message": "这是图片点选验证码（"
                + (cap["text"][:40] or "看题勾选图片并拖到下方")
                + "），需要手动勾选图片，自动过滑块帮不上",
            }
        return await auto_slide_page(self.page, key=self.key)

    async def handoff_slide(self, reason: str = "") -> bool:
        """手动接管：让正在跑的自动过滑块立即收手（包括后台聊天触发的那次）。

        合作式中止：不 cancel 任务，拖拽循环自己退出并松开按键，也不会把
        正在等回答的聊天请求一带打崩。返回确实有自动拖动被叫停。
        """
        hit = request_slide_handoff(self.key)
        if hit:
            logger.info(f"[live:{self.key}] 已请求自动过滑块让位{('：' + reason) if reason else ''}")
            # 调用方紧接着就要把用户的 mousePressed 发进去：先等自动拖动真的抬手，
            # 否则它的 mouse.up 会落在用户按下之后，把这次手动拖拽直接抬掉
            if not await wait_slide_release(self.key):
                logger.warning(f"[live:{self.key}] 自动拖动未在时限内松开左键，手动接管可能不跟手")
        return hit

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
            # 关键：与后台聊天用**同一个标签页**。以前 live 独占一个 tab，
            # 验证码/弹窗出现在 capture tab 上，用户在 /live 画面里根本看不到，
            # 只能去 Chrome 窗口里找另一个标签，极易误以为“没弹验证码”。
            page = await self.pool.get_page(key, "capture")
            cdp = await ctx.new_cdp_session(page)
            sess = LiveSession(key, page, cdp)
            self.sessions[key] = sess
            return sess

    async def open(self, key: str, url: str = "", *, force_navigate: bool = False) -> LiveSession:
        sess = await self.get_or_create(key)
        await sess.start(url, force_navigate=force_navigate)
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


# ---------- 自动过滑块（live 页与后台 capture 页共用） ----------
async def _in_captcha(loc) -> bool:
    """泛化选择器命中的滑块是否真在验证码容器内（防止拖到页面上的普通滑块）。"""
    try:
        return bool(
            await loc.evaluate(
                "el => !!el.closest(\"" + _CAPTCHA_CONTAINER + "\")"
            )
        )
    except Exception:  # noqa: BLE001
        return False


async def _find_slider(page, timeout: float = 8.0):
    """在所有 frame 中找可见的滑块手柄，返回 (locator, 手柄框, 轨道框) 或 None。

    阿里 nc 滑块常在跨域 iframe（punish / baxia）里，Playwright 的
    locator.bounding_box() 返回相对主 frame 视口的坐标，可直接用于鼠标事件。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for fr in page.frames:
            for sel in _SLIDER_SELECTORS:
                try:
                    loc = fr.locator(sel).first
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                    box = await loc.bounding_box()
                    if not box or box["width"] < 8 or box["height"] < 8:
                        continue
                    if sel in _SLIDER_WEAK and not await _in_captcha(loc):
                        continue
                    tbox = None
                    for tsel in _TRACK_SELECTORS:
                        try:
                            tloc = fr.locator(tsel).first
                            if await tloc.count() and await tloc.is_visible():
                                cand = await tloc.bounding_box()
                                if cand and cand["width"] > box["width"]:
                                    tbox = cand
                                    break
                        except Exception:  # noqa: BLE001
                            continue
                    return loc, box, tbox
                except Exception:  # noqa: BLE001
                    continue
        await asyncio.sleep(0.25)
    return None


async def _knob_geometry(loc, box) -> float:
    """计算手柄应拖动的像素距离：轨道宽 - 手柄左偏移 - 手柄宽 - 1。

    `.nc_scale` 等选择器有时只是文字条，比真实轨道窄，直接用会差几十像素；
    这里从手柄向上找「与手柄高度相近、明显更宽」的祖先，取其中最宽的一个作为
    真实轨道（弹窗/页面容器通常高得多，不会误选）。
    """
    try:
        info = await loc.evaluate(
            """el => {
                const er = el.getBoundingClientRect();
                let p = el.parentElement, best = null;
                while (p && p !== document.body && p !== document.documentElement) {
                    const r = p.getBoundingClientRect();
                    const sameRow = Math.abs(r.height - er.height) <= 8;
                    const wider = r.width >= er.width + 20;
                    // 轨道通常不会超过 560px；再宽就是弹窗/整行了，不能当成轨道
                    if (sameRow && wider && r.width <= 560) {
                        if (!best || r.width > best.width) {
                            best = { width: r.width, left: r.left };
                        }
                    }
                    p = p.parentElement;
                }
                if (!best) return null;
                return { trackW: best.width, knobOffset: er.left - best.left, knobW: er.width };
            }"""
        )
    except Exception:  # noqa: BLE001
        return 0.0
    if not info:
        return 0.0
    # 多拖 2-6px：手柄会被 DOM 钳制在轨道末端，宁可轻微过冲也别停在差几像素处
    return max(info["trackW"] - info["knobOffset"] - info["knobW"] - 1, 40.0) + random.uniform(2, 6)


async def _slider_visible(page) -> bool:
    for fr in page.frames:
        for sel in _SLIDER_SELECTORS:
            try:
                loc = fr.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    if sel in _SLIDER_WEAK and not await _in_captcha(loc):
                        continue
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


async def _mask_visible(page) -> bool:
    """baxia 验证弹窗遮罩是否还在（它挡住输入框，是“还需验证”的信号）。"""
    for fr in page.frames:
        try:
            loc = fr.locator(".baxia-dialog-mask").first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def captcha_overlay_visible(page) -> bool:
    """页面上是否真的摆着一个验证码/人机验证容器（用于区分“真验证”和“纯限流”）。"""
    for fr in page.frames:
        try:
            loc = fr.locator(_CAPTCHA_CONTAINER).first
            if await loc.count() and await loc.is_visible():
                box = await loc.bounding_box()
                if box and box["width"] > 60 and box["height"] > 30:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _clip_text(text: str, limit: int = 80) -> str:
    """把弹层文字压成一行（日志/错误信息里只留最关键的一句）。"""
    return " ".join((text or "").split())[:limit]


async def detect_captcha(page) -> dict:
    """识别页面上的验证码题型，返回 {"kind", "text"}。

    - image：图片点选/拖拽类（字节 secsdk 等），只能人工完成；
    - slider：滑块类，可以交给 auto_slide_page 试；
    - unknown：确实摆着验证码容器，但认不出题型；
    - none：页面上没有验证码（多半是纯限流）。
    """
    texts: list[str] = []
    img_count = 0
    for fr in page.frames:
        try:
            got = await fr.evaluate(_OVERLAY_PROBE_JS)
        except Exception:  # noqa: BLE001 - 跨域 iframe / frame 已销毁
            continue
        for item in got or []:
            if not isinstance(item, dict):
                continue
            texts.append(item.get("text") or "")
            img_count = max(img_count, int(item.get("imgs") or 0))
    blob = "\n".join(t for t in texts if t)
    is_image = any(m in blob for m in _IMAGE_CAPTCHA_MARKERS)
    # 结构化兜底：浮层里一排图片 + “提交/拖拽”，即使措辞变了也能认出来
    if not is_image and img_count >= 6 and ("提交" in blob or "拖拽" in blob):
        is_image = True
    if is_image:
        return {"kind": "image", "text": _clip_text(blob)}
    try:
        slider = await _slider_visible(page)
    except Exception:  # noqa: BLE001
        slider = False
    if slider or any(m in blob for m in _SLIDER_CAPTCHA_MARKERS):
        return {"kind": "slider", "text": _clip_text(blob)}
    if await captcha_overlay_visible(page):
        return {"kind": "unknown", "text": _clip_text(blob)}
    return {"kind": "none", "text": _clip_text(blob)}


async def _try_refresh(page) -> bool:
    """验证失败后尝试点“刷新/重试”控件，让滑块重新可拖。"""
    for fr in page.frames:
        for sel in _REFRESH_SELECTORS:
            try:
                loc = fr.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=1500)
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


async def _dump_dialog(page, key: str) -> None:
    """验证失败时把弹窗文本/骨架打到日志，便于定位是“被拒”还是“换了题型”。"""
    for fr in page.frames:
        try:
            info = await fr.evaluate(
                "() => { const el = document.querySelector('.baxia-dialog')"
                " || document.querySelector('[id*=nocaptcha]') || document.querySelector('.nc_scale');"
                " return el ? ((el.innerText || '').trim().slice(0, 200) + ' || ' + el.outerHTML.slice(0, 600)) : ''; }"
            )
            if info:
                logger.warning(f"[{key}] 验证弹窗状态 frame={fr.url[:80]}: {info}")
        except Exception:  # noqa: BLE001
            continue


async def _drag_human(page, sx: float, sy: float, dx: float, key: str = "") -> None:
    """从 (sx, sy) 向右拖 dx 像素。

    人类拖拽的速度曲线是“慢起 → 加速 → 减速”，而不是匀速或快起；
    这里用 easeInOutCubic 位置曲线 + 横向抖动 + 纵向波动 + 偶发停顿 + 末尾微调。
    中途发现人在 /live 里动手（handoff）就立即收手：不再做末端对准，
    直接把当前进度停下并抬起按键 —— 拖到目标点再松手等于没让位。
    """
    slot = key or ""
    pressed = False
    give_up = False
    try:
        # 先悬停到滑块上再按下，部分验证码会统计 mouseenter 后的轨迹
        await page.mouse.move(sx - 2, sy - 1)
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await page.mouse.move(sx, sy)
        await asyncio.sleep(random.uniform(0.08, 0.2))
        # 按下也放在 try 里：取消/异常落在 down 与循环之间时，finally 才能补上 up
        _slide_dragging.add(slot)
        pressed = True
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.12, 0.26))  # 按下后短暂停顿再启动

        steps = random.randint(42, 64)
        x = 0.0
        for i in range(1, steps + 1):
            if _handoff_requested(key):
                logger.info(f"[{key or 'page'}] 检测到手动操作，自动拖动中途让位")
                give_up = True
                break
            t = i / steps
            if t < 0.5:
                eased = 4 * t ** 3                    # 慢起（前 10% 位移很小）
            else:
                eased = 1 - (-2 * t + 2) ** 3 / 2     # 减速收敛
            pos = dx * eased + random.uniform(-1.2, 1.2)
            # 大体单调，允许偶尔回退 0.5px（人手抖动）
            pos = min(max(pos, x - 0.5), dx)
            x = pos
            y = sy + math.sin(t * math.pi * random.uniform(0.8, 1.6)) * random.uniform(0.4, 2.0)
            await page.mouse.move(sx + x, y)
            delay = random.uniform(0.008, 0.02)
            if random.random() < 0.18:
                delay += random.uniform(0.03, 0.09)   # 偶发停顿，更像人手
            await asyncio.sleep(delay)

        if not give_up:
            # 末端微调：回拉 1-2px 再对齐，模拟“对准”动作
            await page.mouse.move(sx + dx - random.uniform(1, 2.5), sy + random.uniform(-0.5, 0.5))
            await asyncio.sleep(random.uniform(0.03, 0.07))
            await page.mouse.move(sx + dx, sy)
            await asyncio.sleep(random.uniform(0.08, 0.16))  # 松开前停一下
    finally:
        # 无论正常结束、异常还是中途让位，都必须抬起按键（Playwright 记得最后坐标），
        # 否则页面一直处于拖拽态，人手接管时“不跟手”
        if pressed:
            try:
                await page.mouse.up()
            except Exception:  # noqa: BLE001
                pass
        _slide_dragging.discard(slot)


# 站点直接判失败（不是“没拖到位”）的文案：出现它说明已被风控，刷新重拖没有意义
# 只收「已经判负」的确定字样；「请重试」「操作太频繁」这类泛化词会误伤正常题干/限流提示
_CAPTCHA_FAIL_MARKERS = ("验证失败", "点击框体重试", "verifyfail", "verify fail", "click to retry")

# 手动接管信号（合作式中止）：不 cancel 任务，避免把后台聊天那条请求一起带崩
_slide_running: dict[str, int] = {}
_slide_handoff: set[str] = set()
# 此刻真的按着左键在拖的 key：手动接管前要等它空出来（一个页面只有一只鼠标）
_slide_dragging: set[str] = set()


def request_slide_handoff(key: str) -> bool:
    """让正在跑的自动过滑块立刻收手（人在画面里动手时调用）。

    拖拽循环与结果轮询都会检查这个标志，最多一个采样周期就停下并松开按键；
    按 key（provider）粒度登记，所以 /live 按钮触发的和后台聊天触发的拖动都能让位。
    返回确实有自动拖动在跑。
    """
    if not key or key not in _slide_running:
        return False
    _slide_handoff.add(key)
    return True


def _handoff_requested(key: str) -> bool:
    return bool(key) and key in _slide_handoff


def _has_fail_marker(text: str) -> bool:
    """弹窗文案是否表示“已被判失败”（而不是“还没拖到位”）。"""
    return bool(text) and any(m in text.lower() for m in _CAPTCHA_FAIL_MARKERS)


async def wait_slide_release(key: str, timeout: float = 0.8) -> bool:
    """等自动拖动松开左键；返回左键是否真的空出来了。

    每个页面只有一只 Playwright 鼠标：若用户的 mousePressed 抢在自动拖动的
    mouse.up 之前落下，那只 up 会把用户的拖拽抬起来，表现就是“手动拖不跟手”。
    所以让位标志置起来后还得等它退出按下区间（停在结果轮询阶段时不按住键，立即返回）。
    """
    slot = key or ""
    deadline = time.monotonic() + timeout
    while slot in _slide_dragging and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return slot not in _slide_dragging


async def _captcha_text(page) -> str:
    """读出验证码容器文案（“拖动到最右边”/“完成拼图”/“验证失败”等）。

    主文档里的 `.baxia-dialog` 往往只有关闭按钮「X」，真正的提示（如
    “验证失败，点击框体重试(error:xxx)”）在 punish 子 frame 里；所以优先返回
    带失败字样的文案，其余情况取最长的一条，纯符号直接跳过。
    """
    best = ""
    for fr in page.frames:
        try:
            txt = await fr.evaluate(
                "() => { const el = document.querySelector('.nc_scale') || document.querySelector('.baxia-dialog')"
                " || document.querySelector('[id*=nocaptcha]'); return el ? (el.innerText || '').trim().slice(0, 200) : ''; }"
            )
        except Exception:  # noqa: BLE001
            continue
        cand = " ".join((txt or "").split())[:120]
        if not cand or not cand.strip("Xx×✕ "):
            continue  # 只有关闭按钮，不是提示文案
        if _has_fail_marker(cand):
            return cand
        # 比较与返回用同一个 120 窗口，免得选出长文案后又被截掉尾巴
        if len(cand) > len(best):
            best = cand
    return best


async def auto_slide_page(page, key: str = "", find_timeout: float = 8.0, verify_timeout: float = 6.0,
                         max_rounds: int = 3) -> dict:
    """对任意 Playwright 页面自动过滑块验证，返回 {ok, message}。

    进这里就把 key 登记为“拖动中”，人在 /live 里动手时 `request_slide_handoff(key)`
    能让它立即让位；退出时清掉计数，保证不会把后来的手动操作误判成接管。
    """
    slot = key or ""
    if slot not in _slide_running:
        # 只有第一个进来的清历史残留信号；并发时先退出的那个不能抹掉别人还没消费的让位
        _slide_handoff.discard(slot)
    _slide_running[slot] = _slide_running.get(slot, 0) + 1
    try:
        return await _auto_slide_rounds(page, key, find_timeout, verify_timeout, max_rounds)
    finally:
        left = _slide_running.get(slot, 1) - 1
        if left > 0:
            _slide_running[slot] = left
        else:
            _slide_running.pop(slot, None)
            _slide_handoff.discard(slot)


async def _auto_slide_rounds(page, key: str, find_timeout: float, verify_timeout: float,
                            max_rounds: int) -> dict:
    """多轮过滑块的实际循环（由 `auto_slide_page` 包住做登记/清理）。

    阿里 baxia 常见多轮验证（过一轮后又弹一轮），故最多连过 max_rounds 轮；
    每轮成功判据为滑块消失，且随后观察一小段时间看是否会再弹。
    """
    handoff = {"ok": False, "handoff": True, "message": "已让位给手动操作（自动拖动已停止，你在画面里拖完就行）"}
    solved = 0
    for rnd in range(max_rounds):
        if _handoff_requested(key):
            return dict(handoff)
        wait = find_timeout if rnd == 0 else 2.5
        try:
            found = await _find_slider(page, wait)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"查找滑块失败: {exc}"}
        if not found:
            if solved:
                # 已过至少一轮；确认验证弹窗是否已彻底关闭
                try:
                    if await _mask_visible(page):
                        if rnd + 1 < max_rounds:
                            if await _try_refresh(page):
                                logger.info(f"[{key or 'page'}] 验证未过，已点刷新重试")
                            await asyncio.sleep(1.0)
                            continue  # 弹窗在但滑块未加载完，再等一轮
                        await _dump_dialog(page, key or 'page')
                        return {"ok": False, "message": "验证弹窗仍在但没有可拖动滑块，请在 /live 手动完成"}
                except Exception:  # noqa: BLE001
                    pass
                return {"ok": True, "message": f"滑块验证通过（共 {solved} 轮）"}
            return {"ok": False, "message": "没找到滑块元素（当前可能没有人机验证）"}
        _loc, box, tbox = found
        sx = box["x"] + box["width"] / 2
        sy = box["y"] + box["height"] / 2
        dx = await _knob_geometry(found[0], box)
        if dx <= 0:
            if tbox:
                # 退化方案：用轨道选择器算（可能因选择器比真实轨道窄而少拖几十像素）
                dx = max(tbox["x"] + tbox["width"] - box["width"] / 2 - 1 - sx, 60.0)
            else:
                dx = 260.0  # 兜底：常见轨道约 260px
        caption = await _captcha_text(page)
        if _has_fail_marker(caption):
            # 弹窗里已经是“验证失败，点击框体重试”：这张题已作废，先点刷新换一张。
            # 换不出新题就别拖了 —— 再拖一次只是又给风控喂一个失败样本。
            if not await _try_refresh(page):
                logger.warning(f"[{key or 'page'}] 验证码处于失败态且没找到刷新控件：{caption!r}")
                return {
                    "ok": False,
                    "message": f"验证码已处于失败态（{caption}）：页面上没找到刷新入口，无法换题；"
                               "请在 /live 手动完成，或等几分钟/换网络后再试",
                }
            logger.info(f"[{key or 'page'}] 验证码处于失败态，已点刷新换题")
            await asyncio.sleep(1.2)
            if _has_fail_marker(await _captcha_text(page)):
                logger.warning(f"[{key or 'page'}] 刷新后仍是失败态，停止自动拖动：{caption!r}")
                return {
                    "ok": False,
                    "message": f"验证码刷新后仍是失败态（{caption}）：当前账号/IP 已被风控，"
                               "请等几分钟或换网络后手动完成",
                }
            # 已换出新题：重走一轮重新定位手柄与轨道，别再用旧的 sx/sy/dx
            continue
        logger.info(
            f"[{key or 'page'}] 自动过滑块 第{rnd + 1}轮: 起点=({sx:.0f},{sy:.0f}) 距离={dx:.0f}px "
            f"轨道={'有' if tbox else '无'} 文案={caption!r}"
        )
        try:
            await _drag_human(page, sx, sy, dx, key=key)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"拖动失败: {exc}"}
        if _handoff_requested(key):
            return dict(handoff)
        # 等本轮到结果：滑块与验证弹窗都消失，且连续两次采样稳定，避免换题瞬间误判通过
        gone = False
        stable = 0
        failed_text = ""
        fails = 0
        for sample in range(max(int(verify_timeout / 0.5), 1)):
            await asyncio.sleep(0.5)
            if _handoff_requested(key):
                return dict(handoff)
            try:
                clear = not await _slider_visible(page) and not await _mask_visible(page)
            except Exception:  # noqa: BLE001
                clear = True  # 页面已跳转，视为通过
            if clear:
                stable += 1
                if stable >= 2:
                    gone = True
                    break
            else:
                stable = 0
                # 隔一次采样再读文案：_captcha_text 要遍历所有 frame，别把 6 秒窗口拖成十几秒
                if sample % 2 == 0:
                    cand = await _captcha_text(page)
                    if _has_fail_marker(cand):
                        # 站点直接判失败（verifyFail）时刷新重拖不会变好；
                        # 要求连续两次都命中，避开站点异步校验中的中间态被误判
                        fails += 1
                        if fails >= 2:
                            failed_text = cand
                            break
                    else:
                        fails = 0
        if failed_text:
            await _dump_dialog(page, key or 'page')
            logger.warning(f"[{key or 'page'}] 第{rnd + 1}轮被判失败：{failed_text!r}（已触发风控，停止重试）")
            return {
                "ok": False,
                "message": f"站点直接判失败（{failed_text}）：当前账号/IP 已触发风控，已停止自动重试。"
                           "建议先在 /live 手动完成，或等几分钟/换网络后再试",
            }
        if not gone:
            await _dump_dialog(page, key or 'page')
            # 被判定轨迹异常时滑块不会消失；点刷新重开一轮通常能过，别直接放弃
            if rnd + 1 < max_rounds and await _try_refresh(page):
                logger.info(f"[{key or 'page'}] 第{rnd + 1}轮验证未过，已点刷新重试")
                await asyncio.sleep(1.2)
                continue
            return {"ok": False, "message": "滑块仍在：可能被判定为非人工轨迹，请重试"}
        solved += 1
        caption = await _captcha_text(page)
        logger.info(f"[{key or 'page'}] 第{rnd + 1}轮滑块已消失，弹窗文案={caption!r}")
        # 给页面留出回流/二轮弹窗时间，避免错过下一轮验证
        await asyncio.sleep(1.6)
    return {"ok": True, "message": f"滑块验证通过（连续 {solved} 轮，未再弹验证）"}
