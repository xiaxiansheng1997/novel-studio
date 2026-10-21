"""内嵌实时浏览器路由：WebSocket 推帧 + 回传输入；页面里完成登录与接口录制。

连接：ws://<host>/ws/live/<key>?url=<可选覆盖地址>&token=<W2A_API_KEY，若已设置>
服务端下发：{t:'meta'|'frame'|'record_result'|'error'|'log', ...}
客户端上行：{t:'mouse'|'wheel'|'key'|'text'|'navigate'|'reload'|'record'|'close', ...}

安全：本机自用仍可能被任意网页跨站访问，故对 WS/REST 做 Origin 校验；
若设了 W2A_API_KEY，则 WS 需带 ?token=、REST 需带 Authorization: Bearer；
navigate / ?url= 仅允许 http(s)，硬拒 file://、chrome://、devtools:// 等。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import urlparse

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger

from app.browser.live_browser import VIEWPORT_H, VIEWPORT_W, LiveBrowserManager, LiveSession
from app.browser.stream_capture import clear_risk, trigger_send
from app.browser.ui_modes import apply_mode, probe_ui
from app.core.modes import resolve_mode

router = APIRouter()

_MOUSE_TYPE = {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}


# ---------- 安全校验 ----------
def _allowed_origins(settings) -> set[str]:
    hosts = {"127.0.0.1", "localhost"}
    if settings.host and settings.host not in ("0.0.0.0", "::"):
        hosts.add(settings.host)
    out: set[str] = set()
    for h in hosts:
        out.add(f"http://{h}:{settings.port}")
        out.add(f"https://{h}:{settings.port}")
    return out


def _origin_ok(origin: str | None, settings) -> bool:
    # 浏览器发起的 WS/跨域 fetch 必带 Origin；无 Origin（curl/同源脚本）放行
    return origin is None or origin in _allowed_origins(settings)


def _key_ok(provided: str | None, settings) -> bool:
    if not settings.api_key:
        return True
    return provided == settings.api_key


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:] if h.lower().startswith("bearer ") else None


def _url_allowed(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


# ---------- REST ----------
@router.get("/live/loggedin/{key}")
async def live_loggedin(request: Request, key: str):
    """探测某 provider 当前是否已登录（仅当浏览器已打开时检查，不启动浏览器）。"""
    settings = request.app.state.settings
    if not _origin_ok(request.headers.get("origin"), settings) or not _key_ok(_bearer(request), settings):
        return JSONResponse(status_code=403, content={"error": {"message": "forbidden", "type": "forbidden"}})
    return await request.app.state.sessions.is_logged_in(key)


@router.post("/live/close/{key}")
async def live_close(request: Request, key: str, full: bool = False):
    """关闭内嵌实时会话（停止推屏/录制）。

    full=true 时额外关闭整个浏览器 context（连宿主窗口一起关）；
    持久化 profile 仍保留，下次聊天会按需重新拉起。
    """
    settings = request.app.state.settings
    if not _origin_ok(request.headers.get("origin"), settings) or not _key_ok(_bearer(request), settings):
        return JSONResponse(status_code=403, content={"error": {"message": "forbidden", "type": "forbidden"}})
    live: LiveBrowserManager = request.app.state.live
    sess = live.get(key)
    if sess and sess.recorder:
        sess.record_stop()
    await live.close(key)  # 走管理器：从注册表摘除 + 彻底回收（停屏/detach/落盘录制）
    if full:
        await live.pool.close(key)
    return {"closed": True, "key": key, "browser_closed": full}


@router.post("/live/cooldown/clear/{key}")
async def live_clear_cooldown(request: Request, key: str):
    """用户已在 /live 完成人机验证后，手动清除该 provider 的风控冷却。"""
    settings = request.app.state.settings
    if not _origin_ok(request.headers.get("origin"), settings) or not _key_ok(_bearer(request), settings):
        return JSONResponse(status_code=403, content={"error": {"message": "forbidden", "type": "forbidden"}})
    clear_risk(key)
    return {"cleared": True, "key": key}


@router.get("/live/debug/{key}")
async def live_debug(request: Request, key: str):
    """登录/风控诊断：页面地址、登录判定、cookie 名与 localStorage 键名（均不含值）。"""
    settings = request.app.state.settings
    if not _origin_ok(request.headers.get("origin"), settings) or not _key_ok(_bearer(request), settings):
        return JSONResponse(status_code=403, content={"error": {"message": "forbidden", "type": "forbidden"}})
    sessions = request.app.state.sessions
    live: LiveBrowserManager = request.app.state.live
    sess = live.get(key)
    info: dict = {"key": key, "live_open": sess is not None}
    info["login"] = await sessions.is_logged_in(key)
    if sess is not None:
        page = sess.page
        info["url"] = page.url
        try:
            info["title"] = await page.title()
        except Exception as exc:  # noqa: BLE001
            info["title_error"] = str(exc)
        try:
            info["localstorage_keys"] = await page.evaluate(
                "() => Object.keys(window.localStorage || {})"
            )
        except Exception as exc:  # noqa: BLE001
            info["localstorage_error"] = str(exc)
    return info


def _wanted_labels(pcfg) -> list[str]:
    """配置里已声明的模型名与开关文案：探测时优先展示，免得在 60 个候选里翻。"""
    if pcfg is None:
        return []
    out: list[str] = []
    out += [m.label for m in pcfg.models if getattr(m, "label", "")]
    out += [s.label for s in pcfg.modes.values() if getattr(s, "label", "")]
    for ctl in pcfg.ui.values():
        out += list(ctl.labels or [])
    # 去重保序
    return list(dict.fromkeys([x for x in out if x]))


@router.get("/live/ui-probe/{key}")
async def live_ui_probe(request: Request, key: str):
    """列出页面上的可点控件与当前状态，供「页面模式」面板校准文案。"""
    settings = request.app.state.settings
    if not _origin_ok(request.headers.get("origin"), settings) or not _key_ok(_bearer(request), settings):
        return JSONResponse(status_code=403, content={"error": {"message": "forbidden", "type": "forbidden"}})
    live: LiveBrowserManager = request.app.state.live
    sess = live.get(key)
    if sess is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"message": "live 未打开", "type": "not_open"}, "hint": "请先在 /live 打开该 provider"},
        )
    config = getattr(request.app.state, "config", None)
    pcfg = config.providers.get(key) if config else None
    items = await probe_ui(sess.page, _wanted_labels(pcfg))
    return {"key": key, "url": sess.page.url, "items": items}


@router.post("/live/mode/{key}")
async def live_set_mode(request: Request, key: str):
    """在 /live 页面上立即切到指定模式（校准与手动控制用）。"""
    settings = request.app.state.settings
    if not _origin_ok(request.headers.get("origin"), settings) or not _key_ok(_bearer(request), settings):
        return JSONResponse(status_code=403, content={"error": {"message": "forbidden", "type": "forbidden"}})
    live: LiveBrowserManager = request.app.state.live
    sess = live.get(key)
    if sess is None:
        return JSONResponse(
            status_code=409,
            content={"error": {"message": "live 未打开", "type": "not_open"}, "hint": "请先在 /live 打开该 provider"},
        )
    reg = getattr(request.app.state, "registry", None)
    adapter = reg.get(key) if reg else None
    if adapter is None:
        return JSONResponse(status_code=404, content={"error": {"message": f"未知 provider: {key}", "type": "not_found"}})
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 空 body 就用默认模型
        body = {}
    body = body if isinstance(body, dict) else {}
    model = str(body.get("model") or (adapter.cfg.model_ids() or [key])[0])
    plan = resolve_mode(
        adapter.cfg,
        SimpleNamespace(
            model=model,
            mode=body.get("mode"),
            thinking=body.get("thinking"),
            search=body.get("search"),
            reasoning_effort=body.get("reasoning_effort"),
            web_search_options=body.get("web_search_options"),
        ),
    )
    res = await apply_mode(sess.page, key, plan, adapter.cfg)
    adapter._last_mode_status = res  # noqa: SLF001 - 与适配器共享状态，供管理页展示
    return {
        "key": key,
        "model": model,
        "mode": plan.preset_id,
        "features": plan.features,
        "model_label": plan.model_label,
        **res,
    }


# ---------- WebSocket 输入分发 ----------
async def _handle(live: LiveBrowserManager, sess: LiveSession, ws: WebSocket, data: dict) -> None:
    t = data.get("t")
    if t == "mouse":
        action = data.get("action", "move")
        cc = data.get("clickCount")
        await sess.mouse(
            _MOUSE_TYPE.get(action, "mouseMoved"),
            float(data.get("x", 0)),
            float(data.get("y", 0)),
            data.get("button"),
            int(cc) if cc is not None else None,
        )
    elif t == "wheel":
        await sess.wheel(float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("dx", 0)), float(data.get("dy", 0)))
    elif t == "key":
        mods = data.get("mods") or {}
        mask = (1 if mods.get("alt") else 0) | (2 if mods.get("ctrl") else 0) | (4 if mods.get("meta") else 0) | (8 if mods.get("shift") else 0)
        await sess.press_key(
            data.get("etype", "keyDown"),
            data.get("key", ""),
            data.get("code", ""),
            data.get("text", ""),
            mask,
        )
    elif t == "text":
        await sess.insert_text(data.get("value", ""))
    elif t == "send":
        # 辅助输入“输入 + 发送”一体：复用后台抓包的 trigger_send，
        # 它知道各站输入框/发送按钮，且会在回车不生效时点按钮兜底。
        value = str(data.get("value") or "").strip()
        if not value:
            await ws.send_json({"t": "log", "message": "辅助输入为空，未发送"})
            return
        reg = getattr(ws.app.state, "registry", None)
        adapter = reg.get(sess.key) if reg else None
        in_sel = adapter.input_selectors() if adapter else None
        send_sel = adapter.send_selectors() if adapter else None
        page = sess.page

        async def _run_send() -> None:
            try:
                await trigger_send(
                    page,
                    value,
                    input_selectors=in_sel or None,
                    send_selectors=send_sel or None,
                    wait_timeout=10.0,
                    clear_first=True,
                )
                msg = f"✓ 已发送：{value[:40]}"
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[live:{sess.key}] 辅助输入发送失败: {exc}")
                msg = f"发送失败：{exc}"
            try:
                await ws.send_json({"t": "log", "message": msg})
            except Exception:  # noqa: BLE001 - 前端可能已断开
                pass

        # 发送要等输入框渲染 + 回车生效，放后台跑，避免阻塞推帧和其他输入
        asyncio.create_task(_run_send())
    elif t == "navigate":
        url = data.get("url", "")
        if _url_allowed(url):
            await sess.navigate(url)
        else:
            await ws.send_json({"t": "error", "message": "非法地址（仅允许 http/https）"})
    elif t == "reload":
        await sess.reload()
    elif t == "record":
        if data.get("on"):
            sess.record_start()
            await ws.send_json({"t": "log", "message": "已开始录制，请在页面里正常发一条对话（如：你好）"})
        else:
            res = sess.record_stop()
            await ws.send_json({"t": "record_result", "result": res})
    elif t == "autoslide":
        # 连点会并发两个拖拽、轨迹互相污染；已有任务在跑时直接忽略
        if sess._slide_task is not None and not sess._slide_task.done():
            await ws.send_json({"t": "log", "message": "自动过滑块正在进行中，请稍候"})
            return
        # 拖动耗时约 2-3 秒 + 结果轮询，放后台跑，避免阻塞其他输入
        async def _run_slide() -> None:
            try:
                res = await sess.auto_slide()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[live:{sess.key}] 自动过滑块失败: {exc}")
                res = {"ok": False, "message": f"自动过滑块失败: {exc}"}
            if res.get("ok"):
                clear_risk(sess.key)  # 验证通过，自动解除风控冷却
            try:
                await ws.send_json({"t": "autoslide_result", "result": res})
            except Exception:  # noqa: BLE001
                pass
        await ws.send_json({"t": "log", "message": "正在自动拖动滑块…"})
        sess._slide_task = asyncio.create_task(_run_slide())
    elif t == "close":
        await live.close(sess.key)


@router.websocket("/ws/live/{key}")
async def ws_live(ws: WebSocket, key: str) -> None:
    settings = ws.app.state.settings
    # 握手前拒绝：不 accept，直接以 HTTP 错误回绝跨站/无口令连接
    if not _origin_ok(ws.headers.get("origin"), settings):
        await ws.close(code=4403)
        return
    if not _key_ok(ws.query_params.get("token"), settings):
        await ws.close(code=4401)
        return

    await ws.accept()
    live: LiveBrowserManager = ws.app.state.live
    config = ws.app.state.config

    pcfg = config.providers.get(key)
    if pcfg is None:
        await ws.send_json({"t": "error", "message": f"未知 provider: {key}"})
        await ws.close()
        return

    raw_url = ws.query_params.get("url") or ""
    if raw_url and not _url_allowed(raw_url):
        await ws.send_json({"t": "log", "message": "已忽略非法地址，改用默认首页"})
    url = raw_url if _url_allowed(raw_url) else pcfg.home_url

    try:
        # 只有显式传了 url 才强制导航：否则复用已停在站点上的标签页，
        # 避免用户切回 /live 时把正在进行的对话/验证码冲掉
        sess = await live.open(key, url, force_navigate=bool(raw_url))
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[live] 打开失败: {key}")
        await ws.send_json({"t": "error", "message": f"打开浏览器失败: {exc}"})
        await ws.close()
        return

    await ws.send_json({"t": "meta", "key": key, "url": url, "w": VIEWPORT_W, "h": VIEWPORT_H, "dsf": 1})

    q = sess.subscribe()

    async def _pump() -> None:
        while True:
            msg = await q.get()
            await ws.send_json(msg)

    pump_task = asyncio.create_task(_pump())
    try:
        while True:
            data = await ws.receive_json()
            try:
                await _handle(live, sess, ws, data)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[live:{key}] 处理输入失败: {exc}")
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.debug(f"[live:{key}] 连接异常结束")
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        sess.unsubscribe(q)  # 无订阅者时会触发 stopScreencast，省 CPU
