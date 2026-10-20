"""管理/健康路由：会话状态、provider 概览、健康检查、日志、统计。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.request_log import get_logs, get_stats

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/admin/status")
async def admin_status(request: Request):
    """各 provider 的登录/浏览器状态，供管理面板展示。"""
    sessions = request.app.state.sessions
    registry = request.app.state.registry
    status = sessions.status_all()
    for row in status:
        adapter = registry.get(row["provider"])
        row["registered"] = adapter is not None
        row["models"] = [m.id for m in (adapter.default_models() if adapter else [])]
    return {"providers": status}


@router.get("/admin/reload")
async def reload_config(request: Request):
    """重读 config.yaml 与模型列表（不重启即可反映模型变化）。"""
    from app.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    request.app.state.config = cfg
    return {"reloaded": True, "providers": list(cfg.providers.keys())}


@router.get("/admin/logs")
async def request_logs(limit: int = 50):
    """返回最近的请求日志。"""
    return get_logs(limit=limit)


@router.get("/admin/stats")
async def usage_stats():
    """返回聚合统计。"""
    return get_stats()
