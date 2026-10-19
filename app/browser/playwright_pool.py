"""Playwright 常驻浏览器池。

每家一个持久化 profile（storage/browser-profiles/<key>），登录一次后长期复用；
context 懒加载、进程内复用，并按 provider 提供串行锁（DS/豆包必须低并发）。

页面按"用途槽位(slot)"管理，避免相互干扰：
- slot="capture"：聊天适配器驱动发送、旁路捕获响应用的页面。
- slot="live"   ：内嵌实时浏览器（登录/录制）用的页面。
- slot="recon"  ：命令行 recon 兜底用的页面（与 live 隔离，避免抢同一页）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger

# 轻量反自动化检测初始化脚本（非万能，配合有头 + 真实 profile 使用）
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


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
            if headless is not None and headless != self._ctx_headless.get(key):
                # context 已按另一种 headless 打开，此处请求不一致：不重建，仅告警
                logger.warning(
                    f"context 已存在，headless 请求不一致(已开={self._ctx_headless.get(key)} 请求={headless})：provider={key}"
                )
            return self._contexts[key]
        async with self._global_lock:
            if key in self._contexts:
                return self._contexts[key]
            pw = await self._ensure_playwright()
            hl = self._default_headless if headless is None else headless
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir(key)),
                headless=hl,
                viewport={"width": 1280, "height": 800},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            await ctx.add_init_script(_STEALTH_JS)
            self._contexts[key] = ctx
            self._ctx_headless[key] = hl
            logger.info(f"浏览器 context 已启动: provider={key} headless={hl}")
            return ctx

    async def get_page(self, key: str, slot: str = "capture"):
        """取某 provider 某用途槽位的页面；不存在或已关闭则新建（按槽位加锁防并发重复创建）。"""
        lock = self._page_locks.get((key, slot))
        if lock is None:
            lock = self._page_locks[(key, slot)] = asyncio.Lock()
        async with lock:
            ctx = await self.context(key)
            page = self._pages.get((key, slot))
            if page is None or page.is_closed():
                page = await ctx.new_page()
                self._pages[(key, slot)] = page
            return page

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
