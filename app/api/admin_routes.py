"""管理/健康路由：会话状态、provider 概览、健康检查、日志、统计。

除 `/health` 外全部要求口令（与 /v1 共用 `app.core.auth.verify_key`）：
这些接口能读出 cookie 名、最近请求与会话列表，不该对局域网/任意网页开放。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import verify_key
from app.core.request_log import get_logs, get_stats

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/admin/status", dependencies=[Depends(verify_key)])
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


@router.post("/admin/reload", dependencies=[Depends(verify_key)])
async def reload_config(request: Request):
    """重读 config.yaml 并**就地**应用到已运行的适配器（不重启服务）。

    注：`.env` 里的 `W2A_*` 是进程启动时读入的，改端口/口令仍需重启。
    """
    from app.config import get_config

    get_config.cache_clear()
    cfg = get_config()
    reg = request.app.state.registry
    summary = reg.apply_config(cfg)
    request.app.state.config = cfg
    # 新增的 provider 需要跑一次 start()（默认空实现，保留钩子语义）
    for key in summary["added"]:
        adapter = reg.get(key)
        if adapter is not None:
            try:
                await adapter.start()
            except Exception:  # noqa: BLE001 - 单个 provider 启动失败不影响其他
                pass
    return {
        "reloaded": True,
        "providers": list(cfg.providers),
        **summary,
        "models": {k: len(v.models) for k, v in cfg.providers.items()},
        "modes": {k: len(v.modes) for k, v in cfg.providers.items()},
    }


@router.get("/admin/reload", dependencies=[Depends(verify_key)])
async def reload_config_get():
    """旧客户端（GET）兼容：明确告知改用 POST，而不是默默什么都不做。"""
    raise HTTPException(status_code=405, detail="请改用 POST /admin/reload")


@router.get("/admin/logs", dependencies=[Depends(verify_key)])
async def request_logs(limit: int = 50):
    """返回最近的请求日志。"""
    return get_logs(limit=limit)


@router.get("/admin/stats", dependencies=[Depends(verify_key)])
async def usage_stats():
    """返回聚合统计。"""
    return get_stats()
