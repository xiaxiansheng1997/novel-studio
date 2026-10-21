"""Playwright 常驻浏览器池。

每家一个持久化 profile（storage/browser-profiles/<key>），登录一次后长期复用；
context 懒加载、进程内复用，并按 provider 提供串行锁（DS/豆包必须低并发）。

页面按"用途槽位(slot)"管理：
- slot="capture"：聊天适配器驱动发送、旁路捕获响应用的页面；
  内嵌实时浏览器（/live，登录/录制）也直接用这个页面 —— 同一个标签页，
  用户在画面里才能看到后台发消息时弹出的验证码。
- slot="recon"  ：命令行 recon 兜底用的页面（与 capture 隔离，避免抢同一页）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger

# 轻量反自动化检测初始化脚本（非万能，配合有头 + 真实 profile 使用）
_STEALTH_JS = """
// 真实 Chrome 的 navigator.webdriver 是 false（不是 undefined），返回 undefined 反而异常
// 每段单独 try/catch：某一段被宿主页限制时，后面的补丁仍要生效
try { Object.defineProperty(navigator, 'webdriver', {get: () => false}); } catch (e) {}
try { window.chrome = window.chrome || { runtime: {} }; } catch (e) {}
try { Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']}); } catch (e) {}
// 无头 Chromium 的 plugins/mimeTypes 为空，是常见机器特征；伪装成真实 Chrome 的内置项
try {
(() => {
  const mk = (name, filename, desc) => ({
    name, filename, description: desc, length: 1,
    item: () => null, namedItem: () => null,
  });
  const list = [
    mk('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
    mk('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format'),
  ];
  list.item = (i) => list[i] || null;
  list.namedItem = (n) => list.find((p) => p.name === n) || null;
  list.refresh = () => undefined;
  list[Symbol.toStringTag] = 'PluginArray';
  list.toString = () => '[object PluginArray]';  // Array 自带 toString，不覆盖会被 String() 看穿
  Object.defineProperty(navigator, 'plugins', {get: () => list});

  const mimes = [
    { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
    { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
  ];
  mimes.item = (i) => mimes[i] || null;
  mimes.namedItem = (n) => mimes.find((m) => m.type === n) || null;
  mimes[Symbol.toStringTag] = 'MimeTypeArray';
  mimes.toString = () => '[object MimeTypeArray]';
  Object.defineProperty(navigator, 'mimeTypes', {get: () => mimes});
})();
} catch (e) {}
// 无头默认走 SwiftShader 软件渲染，暴露真实显卡型号更接近普通用户
try {
(() => {
  const patch = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return 'Google Inc. (Intel)';
      if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return orig.call(this, p);
    };
  };
  patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
})();
} catch (e) {}
"""

# 无头模式默认 UA 带 "HeadlessChrome"，阿里 baxia 风控会直接判机器；
# 这里统一覆盖成真实桌面 Chrome UA（与 Playwright 自带的 Chromium 131 对齐）。
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 仅改 UA 字符串不够：Chromium 无头版会把 UA Client Hints（sec-ch-ua 请求头
# 与 navigator.userAgentData）继续报成 "HeadlessChrome"，baxia 照样识别。
# 必须用 CDP 连元数据一起覆盖，才能让请求头与 JS 侧都显示正常 Chrome。
_UA_METADATA = {
    "brands": [
        {"brand": "Not_A Brand", "version": "24"},
        {"brand": "Chromium", "version": "131"},
        {"brand": "Google Chrome", "version": "131"},
    ],
    "fullVersionList": [
        {"brand": "Not_A Brand", "version": "24.0.0.0"},
        {"brand": "Chromium", "version": "131.0.6778.33"},
        {"brand": "Google Chrome", "version": "131.0.6778.33"},
    ],
    "fullVersion": "131.0.6778.33",
    "platform": "Windows",
    "platformVersion": "15.0.0",
    "architecture": "x86",
    "model": "",
    "mobile": False,
    "bitness": "64",
    "wow64": False,
}


def _is_dead_error(exc: Exception) -> bool:
    """异常是否表示“浏览器/context/页面已关闭”（可靠重启恢复）。"""
    text = str(exc).lower()
    return any(
        hint in text
        for hint in (
            "has been closed",
            "target page, context or browser has been closed",
            "target closed",
            "browser has been closed",
            "connection closed",
        )
    )


class BrowserPool:
    def __init__(self, storage_dir: Path, default_headless: bool = False):
        self._storage_dir = storage_dir / "browser-profiles"
        self._default_headless = default_headless
        self._pw = None  # playwright.async_api.Playwright
        self._contexts: dict[str, object] = {}
        self._ctx_headless: dict[str, bool] = {}
        self._pages: dict[tuple[str, str], object] = {}
        self._page_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def profile_dir(self, key: str) -> Path:
        d = self._storage_dir / key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _ensure_playwright(self):
        if self._pw is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
        return self._pw

    async def context(self, key: str, *, headless: Optional[bool] = None):
        """获取（必要时创建）某 provider 的持久化 BrowserContext。"""
        if key in self._contexts:
            ctx = self._contexts[key]
            if not self._alive(ctx):
                # 用户手动关掉窗口 / 浏览器崩溃：缓存里的 context 已死，必须重启
                logger.warning(f"检测到 {key} 的浏览器已关闭，丢弃缓存重新启动")
                self._purge(key)
            else:
                if headless is not None and headless != self._ctx_headless.get(key):
                    # context 已按另一种 headless 打开，此处请求不一致：不重建，仅告警
                    logger.warning(
                        f"context 已存在，headless 请求不一致(已开={self._ctx_headless.get(key)} 请求={headless})：provider={key}"
                    )
                return ctx
        async with self._global_lock:
            if key in self._contexts:
                return self._contexts[key]
            pw = await self._ensure_playwright()
            hl = self._default_headless if headless is None else headless
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir(key)),
                headless=hl,
                viewport={"width": 1280, "height": 800},
                user_agent=REAL_UA,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--lang=zh-CN",
                ],
            )
            await ctx.add_init_script(_STEALTH_JS)
            self._contexts[key] = ctx
            self._ctx_headless[key] = hl
            # 窗口被手动关掉时要主动从缓存里除名，否则后续请求会一直撞
            # “Target page, context or browser has been closed”，直到重启后端
            ctx.on("close", lambda _ctx=None: self._purge(key))
            logger.info(f"浏览器 context 已启动: provider={key} headless={hl}")
            return ctx

    @staticmethod
    def _alive(ctx) -> bool:
        """非持久化 context 可以用 browser.is_connected() 探活。

        注意：launch_persistent_context 返回的 context 其 browser 为 None，探不
        出死活；那种情况靠上面的 close 事件除名 + get_page 失败重试兜底。
        """
        try:
            browser = ctx.browser
        except Exception:  # noqa: BLE001
            return False
        if browser is None:
            return True
        try:
            return bool(browser.is_connected())
        except Exception:  # noqa: BLE001
            return False

    def _purge(self, key: str) -> None:
        """丢掉已死的 context 与其页面缓存（不 await close，它已经关了）。"""
        self._contexts.pop(key, None)
        self._ctx_headless.pop(key, None)
        for kk in [k for k in self._pages if k[0] == key]:
            self._pages.pop(kk, None)

    async def get_page(self, key: str, slot: str = "capture"):
        """取某 provider 某用途槽位的页面；不存在或已关闭则新建（按槽位加锁防并发重复创建）。"""
        lock = self._page_locks.get((key, slot))
        if lock is None:
            lock = self._page_locks[(key, slot)] = asyncio.Lock()
        async with lock:
            ctx = await self.context(key)
            page = self._pages.get((key, slot))
            if page is not None and page.is_closed():
                page = None
            if page is None:
                try:
                    page = await ctx.new_page()
                except Exception as exc:  # noqa: BLE001
                    # 可能窗口刚被关掉但 close 事件还没送达：重试一次而不是直接失败
                    if not _is_dead_error(exc):
                        raise
                    logger.warning(f"{key} 的 context 已失效（{exc}），重新启动一次")
                    self._purge(key)
                    ctx = await self.context(key)
                    page = await ctx.new_page()
                await self.prepare_page(page)
                self._pages[(key, slot)] = page
            return page

    async def prepare_page(self, page) -> None:
        """给页面补齐反检测指纹（UA Client Hints / WebGL 等在导航前生效）。

        launch 时的 user_agent 只改 UA 字符串，sec-ch-ua 与 userAgentData 仍暴露
        HeadlessChrome；这里用 CDP 连同 userAgentMetadata 一起覆盖，并保活会话。
        """
        try:
            # 重复调用（如重新登录）时先释放旧会话，避免 CDP 会话累积
            old = getattr(page, "_w2a_ua_cdp", None)
            if old is not None:
                try:
                    await old.detach()
                except Exception:  # noqa: BLE001
                    pass
            cdp = await page.context.new_cdp_session(page)
            await cdp.send(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": REAL_UA,
                    "acceptLanguage": "zh-CN,zh;q=0.9",
                    "platform": "Win32",
                    "userAgentMetadata": _UA_METADATA,
                },
            )
            # 保住 CDP 会话引用，避免被回收导致覆盖失效
            page._w2a_ua_cdp = cdp
        except Exception as exc:  # noqa: BLE001 - 指纹增强失败不应阻断主流程
            logger.debug(f"UA 指纹覆盖失败（不影响主流程）: {exc}")

    async def close_page(self, key: str, slot: str) -> None:
        page = self._pages.pop((key, slot), None)
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:  # noqa: BLE001
                pass

    def is_open(self, key: str) -> bool:
        return key in self._contexts

    async def close(self, key: str) -> None:
        for (k, slot) in [kk for kk in self._pages if kk[0] == key]:
            await self.close_page(k, slot)
        ctx = self._contexts.pop(key, None)
        self._ctx_headless.pop(key, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        for key in list(self._contexts.keys()):
            await self.close(key)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
