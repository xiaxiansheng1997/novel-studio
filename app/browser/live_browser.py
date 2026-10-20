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
_SLIDER_WEAK = (
    "[id*='n1z']",
    ".verify-move-block",
    ".slider-btn",
)
# 验证码滑块/轨道的常见选择器（阿里 nc、腾讯、通用命名），按优先级尝试
_SLIDER_SELECTORS = _SLIDER_STRONG + _SLIDER_WEAK
# 泛化滑块必须位于这些容器之一（阿里 nc / 腾讯 / 通用 captcha）
_CAPTCHA_CONTAINER = (
    ".nc_scale,[id*='nocaptcha'],.baxia-dialog,[id*='punish'],"
    ".verify-bar-area,.slider-track,.verify-wrap,[class*='captcha'],[id*='captcha']"
)
_TRACK_SELECTORS = (
    ".nc_scale",
    "#nc_1_wrapper",
    "[id*='scale_text']",
    ".verify-bar-area",
    ".slider-track",
)
# 验证失败后“点击刷新/重试”的控件（必须限定在验证码容器内，裸 [id*='refresh'] 会误点页面其他刷新按钮）
_REFRESH_SELECTORS = (
    "#nc_1_refresh",
    ".nc_iconfont.btn_refresh",
    ".baxia-dialog [id*='refresh']",
    "[id*='nocaptcha'] [id*='refresh']",
)

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
        return await auto_slide_page(self.page, key=self.key)

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


async def _drag_human(page, sx: float, sy: float, dx: float) -> None:
    """从 (sx, sy) 向右拖 dx 像素。

    人类拖拽的速度曲线是“慢起 → 加速 → 减速”，而不是匀速或快起；
    这里用 easeInOutCubic 位置曲线 + 横向抖动 + 纵向波动 + 偶发停顿 + 末尾微调。
    """
    # 先悬停到滑块上再按下，部分验证码会统计 mouseenter 后的轨迹
    await page.mouse.move(sx - 2, sy - 1)
    await asyncio.sleep(random.uniform(0.04, 0.12))
    await page.mouse.move(sx, sy)
    await asyncio.sleep(random.uniform(0.08, 0.2))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.12, 0.26))  # 按下后短暂停顿再启动

    steps = random.randint(42, 64)
    x = 0.0
    for i in range(1, steps + 1):
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

    # 末端微调：回拉 1-2px 再对齐，模拟“对准”动作
    await page.mouse.move(sx + dx - random.uniform(1, 2.5), sy + random.uniform(-0.5, 0.5))
    await asyncio.sleep(random.uniform(0.03, 0.07))
    await page.mouse.move(sx + dx, sy)
    await asyncio.sleep(random.uniform(0.08, 0.16))  # 松开前停一下
    await page.mouse.up()


async def _captcha_text(page) -> str:
    """读出验证码容器文案（“拖动到最右边”/“完成拼图”/“验证失败”等）。"""
    for fr in page.frames:
        try:
            txt = await fr.evaluate(
                "() => { const el = document.querySelector('.nc_scale') || document.querySelector('.baxia-dialog')"
                " || document.querySelector('[id*=nocaptcha]'); return el ? (el.innerText || '').trim().slice(0, 120) : ''; }"
            )
            if txt:
                return txt.replace("\n", " / ")
        except Exception:  # noqa: BLE001
            continue
    return ""


async def auto_slide_page(page, key: str = "", find_timeout: float = 8.0, verify_timeout: float = 6.0,
                         max_rounds: int = 3) -> dict:
    """对任意 Playwright 页面自动过滑块验证，返回 {ok, message}。

    阿里 baxia 常见多轮验证（过一轮后又弹一轮），故最多连过 max_rounds 轮；
    每轮成功判据为滑块消失，且随后观察一小段时间看是否会再弹。
    """
    solved = 0
    for rnd in range(max_rounds):
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
        logger.info(
            f"[{key or 'page'}] 自动过滑块 第{rnd + 1}轮: 起点=({sx:.0f},{sy:.0f}) 距离={dx:.0f}px "
            f"轨道={'有' if tbox else '无'} 文案={caption!r}"
        )
        try:
            await _drag_human(page, sx, sy, dx)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"拖动失败: {exc}"}
        # 等本轮到结果：滑块与验证弹窗都消失，且连续两次采样稳定，避免换题瞬间误判通过
        gone = False
        stable = 0
        for _ in range(max(int(verify_timeout / 0.5), 1)):
            await asyncio.sleep(0.5)
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
