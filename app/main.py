"""FastAPI 入口：装配运行时、挂载 OpenAI/管理路由、静态 UI、统一异常处理。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app import __version__
from app.api import admin_routes, live_routes, openai_routes
from app.config import BASE_DIR
from app.core.errors import W2AError
from app.runtime import build_runtime, setup, teardown

WEB_DIR = BASE_DIR / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
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


@app.exception_handler(W2AError)
async def w2a_error_handler(request: Request, exc: W2AError):
    """把内部错误转成 OpenAI 风格的 JSON 错误响应。"""
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"message": exc.message, "type": exc.type, "code": exc.code}},
    )


app.include_router(openai_routes.router)
app.include_router(admin_routes.router)
app.include_router(live_routes.router)


@app.get("/")
async def root():
    return {
        "name": "Web2API 对话反代",
        "version": __version__,
        "openai_base": "/v1",
        "chat_ui": "/ui/chat.html",
        "admin_ui": "/ui/admin.html",
        "live_ui": "/ui/live.html",
        "status": "/admin/status",
    }


# 静态 UI（聊天界面 + 管理面板）
if WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
