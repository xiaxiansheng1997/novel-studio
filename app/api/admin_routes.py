"""管理/健康路由：会话状态、provider 概览、健康检查、日志、统计。

除 `/health` 外全部要求口令（与 /v1 共用 `app.core.auth.verify_key`）：
这些接口能读出 cookie 名、最近请求与会话列表，不该对局域网/任意网页开放。
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.alerts import latest_seq, recent, subscribe, subscriber_count, unsubscribe
from app.core.auth import expected_key, key_matches, origin_ok, verify_key
from app.core.request_log import get_logs, get_stats

router = APIRouter()

# 进程启动时刻，供 /health 报 uptime
STARTED_AT = time.time()


@router.get("/health")
async def health(request: Request, detail: bool = False):
    """公开探活：口令开关 + 各 provider 的浏览器/登录/风控冷却状态。

    默认只报「浏览器开没开」与「风控冷却剩几秒」（纯内存判断，很快）；
    加 `?detail=1` 才去读 cookie 判登录态（会碰页面，适合人工排查/监控低频调用）。
    """
    settings = getattr(request.app.state, "settings", None)
    sessions = getattr(request.app.state, "sessions", None)
    providers: list[dict] = []
    if sessions is not None:
        from app.browser.stream_capture import risk_cooldown_remaining

        for key in list(sessions.config.providers):
            pcfg = sessions.config.providers[key]
            row: dict = {
                "provider": key,
                "enabled": bool(pcfg.enabled),
                "browser_open": sessions.pool.is_open(key),
                "risk_cooldown": risk_cooldown_remaining(key),
            }
            if detail and row["browser_open"]:
                try:
                    info = await sessions.is_logged_in(key)
                    row["logged_in"] = bool(info.get("logged_in"))
                    row["login_via"] = info.get("via") or ""
                except Exception:  # noqa: BLE001 - 探活不能因单个 provider 报错就 500
                    row["logged_in"] = None
            providers.append(row)
    return {
        "status": "ok",
        "auth_required": bool(getattr(settings, "api_key", None)),
        "uptime_sec": int(time.time() - STARTED_AT),
        "browsers_open": sum(1 for p in providers if p["browser_open"]),
        "risk_cooldown": {p["provider"]: p["risk_cooldown"] for p in providers if p["risk_cooldown"]},
        "providers": providers,
    }


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
    # alerts_streams：活着的告警 SSE 连接数。正常应等于「开着几个页面」；
    # 只增不减说明断链没被察觉（见 stream_alerts 里的说明）。
    return {"providers": status, "alerts_streams": subscriber_count()}


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
    # LiveBrowserManager 在构造时就持住了 config 引用，apply_config 不会递到它：
    # 漏了这一句，/live 建页面时读的还是旧的 headless，和后台聊天那条链路（已走
    # adapter.cfg）各说各话，谁先建 context 谁定 —— 改了 headless 看着像没生效。
    live = getattr(request.app.state, "live", None)
    if live is not None:
        live.config = cfg
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
async def request_logs(limit: int = 50, rid: str = ""):
    """返回最近的请求日志；带 `rid` 时按 trace id 过滤（支持前缀）。"""
    return get_logs(limit=limit, rid=rid.strip() or None)


# SSE 心跳间隔：这么久没有告警就发一行注释，免得代理把空闲长连接掐掉
HEARTBEAT_SEC = 15.0


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/admin/alerts", dependencies=[Depends(verify_key)])
async def list_alerts(since: int = 0, limit: int = 20, kind: str = ""):
    """最近的告警事件（验证码/风控/登录失效）；带 `since` 可增量拉取。"""
    return {
        "latest": latest_seq(),
        "items": recent(since=max(0, since), limit=max(1, min(limit, 50)), kind=kind.strip()),
    }


@router.get("/admin/alerts/stream")
async def stream_alerts(request: Request, token: str = "", since: int = -1):
    """SSE 推送告警：EventSource 不能带自定义请求头，口令只能走 ?token=。

    `since=-1` 表示只推“从现在起”的新事件，避免一进页面就弹旧的验证码提醒。
    """
    settings = request.app.state.settings
    if not key_matches(token or None, expected_key(request)):
        raise HTTPException(status_code=401, detail="无效的 API Key")
    if not origin_ok(request.headers.get("origin"), settings):
        raise HTTPException(status_code=403, detail="来源不被允许")

    start = latest_seq() if since < 0 else since
    q = subscribe()
    # 断链只能自己收：本应用外面套着 RequestLogMiddleware（BaseHTTPMiddleware），
    # 它给的 request.is_disconnected() 用「一个立刻取消的 CancelScope」包住 receive，
    # 那个 await 必然被取消 → 永远返回 False。以前这条流在页面关掉/刷新后其实还活着，
    # 每 15s 空打一次心跳，订阅者只增不减（多刷几次就攒一堆僵尸协程）。
    # 这里直接挂在 request.receive() 上等真正的 http.disconnect。
    closed = asyncio.Event()

    async def _watch_disconnect() -> None:
        try:
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    break
        except Exception:  # noqa: BLE001 - 连接被硬拆时 receive 可能直接抛
            pass
        finally:
            closed.set()

    async def _gen():
        watcher = asyncio.create_task(_watch_disconnect())
        getter = stopper = None
        # 先发一条 hello：EventSource 立即进入 open，前端也能确认链路通了
        yield _sse({
            "seq": latest_seq(),
            "kind": "hello",
            "provider": "",
            "message": "",
            "level": "info",
            "ts": time.time(),
        })
        try:
            while not closed.is_set():
                # 同时等「新告警」和「客户端断开」，谁先到办谁；都没到就发心跳
                getter = asyncio.create_task(q.get())
                stopper = asyncio.create_task(closed.wait())
                done, pending = await asyncio.wait(
                    {getter, stopper}, timeout=HEARTBEAT_SEC, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if stopper in done:
                    break
                if getter not in done:
                    yield ": ping\n\n"  # 注释行心跳，防代理把长连接掐掉
                    continue
                item = getter.result()
                if item["seq"] > start:
                    yield _sse(item)
        finally:
            watcher.cancel()
            # 外层被取消时（CancelledError 落在 await asyncio.wait 上）asyncio.wait
            # 不替我们收任务：留着的 q.get() 会从队列里吃掉一条谁也不读的告警。
            for task in (getter, stopper):
                if task is not None and not task.done():
                    task.cancel()
            unsubscribe(q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/admin/stats", dependencies=[Depends(verify_key)])
async def usage_stats():
    """返回聚合统计。"""
    return get_stats()
