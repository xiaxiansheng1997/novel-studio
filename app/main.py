"""FastAPI 入口：装配运行时、挂载 OpenAI/管理路由、静态 UI、统一异常处理。"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app import __version__
from app.api import admin_routes, conversation_routes, live_routes, openai_routes
from app.config import BASE_DIR
from app.core.errors import W2AError
from app.core.request_log import RequestLogMiddleware
from app.runtime import build_runtime, setup, teardown

WEB_DIR = BASE_DIR / "web"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Windows + uvicorn --reload 会切到 SelectorEventLoop，该循环不支持子进程，
    # Playwright 启动浏览器会抛 NotImplementedError（聊天返回空 stream_error）。
    if sys.platform == "win32" and type(asyncio.get_running_loop()).__name__ == "SelectorEventLoop":
        logger.error(
            "当前事件循环为 SelectorEventLoop，Playwright 无法启动浏览器！"
            "请勿使用 `uvicorn --reload`，改用 `python -m uvicorn app.main:app` 或 `python dev.py`"
        )
    rt = build_runtime()
    await setup(rt)
    app.state.settings = rt.settings
    app.state.config = rt.config
    app.state.sessions = rt.sessions
    app.state.store = rt.store
    app.state.registry = rt.registry
    app.state.live = rt.live
    logger.info(
        f"Web2API v{__version__} 已启动 | providers={list(rt.registry.adapters.keys())} "
        f"| http://{rt.settings.host}:{rt.settings.port}/v1"
    )
    try:
        yield
    finally:
        await teardown(rt)
        logger.info("Web2API 已优雅关停")


app = FastAPI(title="Web2API 对话反代", version=__version__, lifespan=lifespan)

# 本机自用，允许本地客户端跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLogMiddleware)


@app.exception_handler(W2AError)
async def w2a_error_handler(request: Request, exc: W2AError):
    """把内部错误转成 OpenAI 风格的 JSON 错误响应。"""
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"message": exc.message, "type": exc.type, "code": exc.code}},
    )


app.include_router(openai_routes.router)
app.include_router(conversation_routes.router)
app.include_router(admin_routes.router)
app.include_router(live_routes.router)


@app.get("/")
async def root():
    return {
        "name": "Web2API 对话反代",
        "version": __version__,
        "openai_base": "/v1",
        "chat_ui": "/ui/chat.html",
        "admin_ui": "/admin",
        "live_ui": "/live",
        "status": "/admin/status",
    }


# 静态 UI：优先挂载 Vue SPA，回退到旧 web/ 目录
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="spa-assets")

    # SPA fallback: 只接管前端路由；API/WS/静态资源未命中时仍返回干净的 404，
    # 否则 GET /v1/typo 之类会返回 200 + HTML，客户端只会得到难定位的“非 JSON”错误。
    _NON_SPA_PREFIXES = ("v1/", "admin/", "live/", "ws/", "assets/")

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        if path.startswith(_NON_SPA_PREFIXES):
            raise HTTPException(status_code=404, detail="Not Found")
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return {"error": "Frontend not built"}

elif WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
