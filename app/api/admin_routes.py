"""管理/健康路由：会话状态、provider 概览、健康检查、日志、统计。

除 `/health` 外全部要求口令（与 /v1 共用 `app.core.auth.verify_key`）：
这些接口能读出 cookie 名、最近请求与会话列表，不该对局域网/任意网页开放。

/admin/compat 一组是直连通道的页面配置：base_url/key/模型存在
sessions.sqlite 的 provider_compat 表（storage/ 在 .gitignore 里，key 不进 git），
保存即重读配置并就地应用，免重启。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
    """各 provider 的登录/浏览器状态，供管理面板展示。

    compat 条目附直连语义字段（base_url/key 状态/ready）：前端卡片靠
    `compat` 分支，不再给直连通道画 Profile/浏览器那三个不相干的点。
    """
    sessions = request.app.state.sessions
    registry = request.app.state.registry
    status = sessions.status_all()
    for row in status:
        adapter = registry.get(row["provider"])
        row["registered"] = adapter is not None
        row["models"] = [m.id for m in (adapter.default_models() if adapter else [])]
        pcfg = registry.config.providers.get(row["provider"])
        row["compat"] = bool(pcfg is not None and getattr(pcfg, "compat", False))
        if row["compat"]:
            row.update(_compat_brief(pcfg, adapter))
    # alerts_streams：活着的告警 SSE 连接数。正常应等于「开着几个页面」；
    # 只增不减说明断链没被察觉（见 stream_alerts 里的说明）。
    return {"providers": status, "alerts_streams": subscriber_count()}


def _storage_dir(request: Request):
    return request.app.state.settings.resolved_storage_dir()


def _mask_key(raw: str) -> str:
    """掩码只报尾 4 位：短 key（测试桩常 <8 位）连尾数都不露，只说「已设置」。"""
    if not raw:
        return ""
    return f"…{raw[-4:]}" if len(raw) > 8 else "已设置"


def _compat_brief(pcfg, adapter) -> dict:
    """compat 条目的对外形状——**永远不含明文 key**。key 来源分四档：
    db（页面配的）/ env（.env 变量已设）/ none（声明了变量却取不到值）/ not_needed（无需鉴权）。"""
    env_val = os.environ.get(pcfg.api_key_env or "", "").strip()
    if pcfg.api_key:
        source = "db"
    elif env_val:
        source = "env"
    elif pcfg.api_key_env:
        source = "none"
    else:
        source = "not_needed"
    ready = bool(adapter.ready()) if adapter is not None and hasattr(adapter, "ready") else (
        # 停用条目没有适配器：按配置算「配好了没」，重新启用后界面不至于谎报没就绪
        bool((pcfg.base_url or "").strip()) and bool(pcfg.models) and (source != "none")
    )
    return {
        "base_url": pcfg.base_url,
        "key_source": source,
        "key_masked": _mask_key(pcfg.api_key) if source == "db" else "",
        "key_env": pcfg.api_key_env,
        "env_configured": bool(env_val),
        "ready": ready,
        "strip_prefix": pcfg.strip_prefix,
    }


# ==================== 直连通道（compat）页面配置 ====================

# key 形状：小写字母开头，字母/数字/连字符，2~31 位。路由按前缀命中，
# 乱键会把别的模型的流量拐进必死通道，入口就得拦住
_COMPAT_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


class CompatCreate(BaseModel):
    key: str
    display_name: str = ""
    base_url: str = ""
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    enabled: bool = True


class CompatUpdate(BaseModel):
    # None = 保持原值；api_key 传空串 = 清除库里 key（回退 .env 通道）
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None
    enabled: Optional[bool] = None


class CompatTest(BaseModel):
    api_key: str = ""


def _yaml_provider_keys() -> set[str]:
    """config.yaml 里原生存在的 provider key（判断 in_yaml / 能不能删）。"""
    import yaml

    from app.config import BASE_DIR

    try:
        raw = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        return set()
    return set((raw.get("providers") or {}).keys())


def _compat_row(request: Request, key: str) -> dict:
    """单条 compat 的对外形状（GET/PUT/POST 返回体共用），附来源标记。"""
    from app.core import provider_store

    reg = request.app.state.registry
    pcfg = reg.config.providers.get(key)
    if pcfg is None or not getattr(pcfg, "compat", False):
        raise HTTPException(status_code=404, detail=f"直连通道不存在: {key}")
    row = {"provider": key, "display_name": pcfg.display_name or key,
           **_compat_brief(pcfg, reg.get(key))}
    row["enabled"] = pcfg.enabled
    row["models"] = [m.id for m in pcfg.models]
    row["in_yaml"] = key in _yaml_provider_keys()
    row["in_db"] = provider_store.get_row(_storage_dir(request), key) is not None
    return row


@router.get("/admin/compat", dependencies=[Depends(verify_key)])
async def list_compat(request: Request):
    """全部直连通道（含停用的）：控制台卡片与编辑弹窗共用。"""
    reg = request.app.state.registry
    return {"providers": [_compat_row(request, k) for k, pcfg in reg.config.providers.items()
                          if getattr(pcfg, "compat", False)]}


@router.post("/admin/compat", dependencies=[Depends(verify_key)])
async def create_compat(body: CompatCreate, request: Request):
    """新建直连通道（DeepSeek 官方、Moonshot、本地 LM Studio 等）。"""
    from app.core import provider_store

    key = body.key.strip().lower()
    if not _COMPAT_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="key 只能用小写字母/数字/连字符，2~31 位且字母开头")
    reg = request.app.state.registry
    reserved = {k for k, pcfg in reg.config.providers.items() if not getattr(pcfg, "compat", False)}
    if key in reserved:
        raise HTTPException(status_code=400, detail=f"key 与反代站点撞车: {key}")
    if key in reg.config.providers or provider_store.get_row(_storage_dir(request), key):
        raise HTTPException(status_code=409, detail=f"直连通道已存在: {key}，请直接编辑")
    provider_store.upsert_row(
        _storage_dir(request),
        key=key,
        display_name=body.display_name,
        base_url=body.base_url,
        api_key=body.api_key,
        models=body.models,
        enabled=body.enabled,
    )
    _apply_runtime_config(request)
    return _compat_row(request, key)


@router.put("/admin/compat/{key}", dependencies=[Depends(verify_key)])
async def update_compat(key: str, body: CompatUpdate, request: Request):
    """局部更新：没传的字段保持原值；api_key 传空串才清除。

    首次保存会把 config.yaml 的出厂值作为底写进库（否则「只改 base_url」
    会把 yaml 里的模型清单弄丢）；yaml 里的 api_key_env 声明不搬进库，
    .env 兜底通道维持由 yaml 说话。
    """
    from app.core import provider_store

    reg = request.app.state.registry
    pcfg = reg.config.providers.get(key)
    if pcfg is None or not getattr(pcfg, "compat", False):
        raise HTTPException(status_code=404, detail=f"直连通道不存在: {key}")
    row = provider_store.get_row(_storage_dir(request), key) or {
        "display_name": pcfg.display_name,
        "base_url": pcfg.base_url,
        "api_key": pcfg.api_key,
        "models": [m.id for m in pcfg.models],
        "enabled": pcfg.enabled,
    }
    fields = body.model_dump(exclude_none=True)
    row.update({k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items()})
    provider_store.upsert_row(
        _storage_dir(request),
        key=key,
        display_name=row["display_name"],
        base_url=row["base_url"],
        api_key=row["api_key"],
        models=list(row["models"]),
        enabled=bool(row["enabled"]),
    )
    _apply_runtime_config(request)
    return _compat_row(request, key)


@router.delete("/admin/compat/{key}", dependencies=[Depends(verify_key)])
async def delete_compat(key: str, request: Request):
    """只删库行；config.yaml 示范条目删不掉（只可停用），删行后回退出厂值。"""
    from app.core import provider_store

    if not provider_store.delete_row(_storage_dir(request), key):
        raise HTTPException(status_code=404, detail="库里没有这条直连通道的配置")
    _apply_runtime_config(request)
    return {"ok": True, "reverted_to_yaml": key in _yaml_provider_keys()}


@router.post("/admin/compat/{key}/test", dependencies=[Depends(verify_key)])
async def test_compat(key: str, request: Request, body: Optional[CompatTest] = None):
    """向上游发一条 1-token 最小请求验连通性。

    弹窗里还没保存的 key 可以先发进来试（带 body.api_key 就用它，不带就用库里已存的）；
    不走 adapter/限流——这是体检，不是生产流量。body 可省：裸 POST 测已存配置
    是常态用法，要求空对象当入参只是给调用方添堵。
    """
    reg = request.app.state.registry
    pcfg = reg.config.providers.get(key)
    if pcfg is None or not getattr(pcfg, "compat", False):
        raise HTTPException(status_code=404, detail=f"直连通道不存在: {key}")
    base = (pcfg.base_url or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=400, detail="先填 base_url（兼容端点根地址，如 https://api.deepseek.com/v1）")
    raw_key = ((body.api_key.strip() if body else "") or pcfg.api_key
               or os.environ.get(pcfg.api_key_env or "", "").strip())
    model = pcfg.models[0].id if pcfg.models else ""
    if not model:
        raise HTTPException(status_code=400, detail="先至少填一个模型 id")
    headers = {"Content-Type": "application/json"}
    if raw_key:
        headers["Authorization"] = f"Bearer {raw_key}"
    payload = {
        "model": model, "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1, "stream": False,
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        ) as client:
            resp = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
        latency = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 200:
            return {"ok": True, "latency_ms": latency, "detail": f"上游通了（{model}）"}
        return {"ok": False, "latency_ms": latency,
                "detail": f"上游 HTTP {resp.status_code}：{(resp.text or '')[:200]}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "latency_ms": int((time.perf_counter() - started) * 1000),
                "detail": f"连不上 {base}：{exc}"}


def _apply_runtime_config(request: Request) -> dict:
    """重读 config.yaml（含 DB 覆盖层）并**就地**应用到已运行的适配器。

    /admin/reload 与直连通道的保存共用这一份：两条路径对「生效」的定义
    必须一模一样，否则页面保存看着成功了、行为却还停在旧配置上。
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
    # 同一类钉子的另一处：/admin/status 的行来自 SessionManager.status_all()，
    # 它遍历的是自己持的 config——不递这一句，新建的直连通道到重启前都不会
    # 出现在卡片列表里（页面保存看着成功，列表不动）。
    sessions = getattr(request.app.state, "sessions", None)
    if sessions is not None:
        sessions.config = cfg
    return summary


@router.post("/admin/reload", dependencies=[Depends(verify_key)])
async def reload_config(request: Request):
    """重读 config.yaml 并就地应用（不重启服务）。"""
    summary = _apply_runtime_config(request)
    cfg = request.app.state.config
    reg = request.app.state.registry
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
