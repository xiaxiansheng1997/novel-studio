"""运行时装配：把配置、浏览器池、会话管理、存储、注册表组装成可共享的 Runtime。

main.py 的 lifespan 与 scripts/* 都通过 build_runtime() 获取依赖，避免重复装配。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.browser.playwright_pool import BrowserPool
from app.browser.session_manager import SessionManager
from app.browser.live_browser import LiveBrowserManager
from app.config import AppConfig, Settings, get_config, get_settings
from app.core.base import AppContext
from app.core.registry import Registry
from app.core.store import ConversationStore


@dataclass
class Runtime:
    settings: Settings
    config: AppConfig
    pool: BrowserPool
    sessions: SessionManager
    live: LiveBrowserManager
    store: ConversationStore
    ctx: AppContext
    registry: Registry


def build_runtime() -> Runtime:
    settings = get_settings()
    config = get_config()
    storage = settings.resolved_storage_dir()

    pool = BrowserPool(storage, default_headless=False)
    sessions = SessionManager(pool, storage, config)
    live = LiveBrowserManager(pool, config)
    store = ConversationStore(storage / "sessions.sqlite")
    ctx = AppContext(config=config, sessions=sessions, store=store, live=live)
    registry = Registry(config, ctx)
    return Runtime(settings, config, pool, sessions, live, store, ctx, registry)


async def setup(rt: Runtime) -> None:
    """异步初始化（需在运行中的事件循环里调用）：建库、启动适配器。"""
    await rt.store.init()
    await rt.registry.start_all()


async def teardown(rt: Runtime) -> None:
    await rt.live.close_all()
    await rt.registry.stop_all()
    await rt.pool.shutdown()
    await rt.store.close()


def run(coro_factory):
    """供脚本用的简单入口。"""

    async def _main():
        rt = build_runtime()
        await setup(rt)
        try:
            return await coro_factory(rt)
        finally:
            await teardown(rt)

    return asyncio.run(_main())
