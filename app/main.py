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
from app.config import BASE_DIR, get_settings
from app.core.auth import allowed_origins
from app.core.errors import W2AError
from app.core.request_log import RequestLogMiddleware, log_patcher
from app.runtime import build_runtime, setup, teardown

# 请求内的日志统一带 [rid] 前缀（patcher 改 record，不挑 sink 格式）
logger.configure(patcher=log_patcher)

_WEB_DIR = BASE_DIR / "web"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"


def _add_file_sink(rt) -> int:
    """日志落盘：storage/logs/web2api_<日期>.log，按天轮转保留 14 天。

    enqueue=True 让写盘不阻塞事件循环（Windows 上尤其明显）。
    """
    log_dir = rt.settings.resolved_storage_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return logger.add(
        str(log_dir / "web2api_{time:YYYY-MM-DD}.log"),
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
        level=rt.config.log_level or "INFO",
    )


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
    sink_id = _add_file_sink(rt)
    app.state.settings = rt.settings
    app.state.config = rt.config
    app.state.sessions = rt.sessions
    app.state.store = rt.store
    app.state.registry = rt.registry
    app.state.live = rt.live
    logger.info(
        f"Web2API v{__version__} 已启动 | providers={list(rt.registry.adapters.keys())} "
        f"| http://{rt.settings.host}:{rt.settings.port}/v1 "
        f"| 日志={rt.settings.resolved_storage_dir() / 'logs'}"
    )
    try:
        yield
    finally:
        await teardown(rt)
        try:
            logger.remove(sink_id)  # 先关文件 sink，避免 teardown 日志写进已关闭的文件
        except ValueError:
            pass
        logger.info("Web2API 已优雅关停")


app = FastAPI(title="Web2API 对话反代", version=__version__, lifespan=lifespan)

# 浏览器跨域只放行本机来源（含 Vite dev 端口）；需要时用 W2A_ALLOW_ORIGINS 追加，
# 设为 * 则完全放开。判断逻辑与 live_routes 的来源校验共用 app.core.auth。
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins(get_settings())),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLogMiddleware)


@app.exception_handler(W2AError)
async def w2a_error_handler(request: Request, exc: W2AError):
    """把内部错误转成 OpenAI 风格的 JSON 错误响应。"""
    headers: dict[str, str] = {}
    # 限流类错误带上 Retry-After，客户端（含 OpenAI SDK）能据此退避
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        headers["Retry-After"] = str(int(retry_after + 0.999))
    # 出错时把 trace id 也回给对方：报障时能直接对上 /admin/logs 与落盘日志
    rid = getattr(request.state, "rid", "")
    if rid:
        headers["X-W2A-Request-Id"] = rid
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"message": exc.message, "type": exc.type, "code": exc.code}},
        headers=headers or None,
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

elif _WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")
