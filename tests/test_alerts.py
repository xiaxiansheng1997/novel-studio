"""告警总线：发布/订阅/增量查询 + /admin/alerts 口令牌。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin_routes import router as admin_router
from app.api.admin_routes import stream_alerts
from app.core import alerts


def _app(api_key: str = "") -> FastAPI:
    app = FastAPI()
    app.state.settings = SimpleNamespace(api_key=api_key, allow_origins="", host="127.0.0.1", port=8000)
    app.include_router(admin_router)
    return app


def test_publish_and_recent() -> None:
    base = alerts.latest_seq()
    a = alerts.publish("captcha", "qwen", "需要人工验证", level="error")
    b = alerts.publish("risk", "doubao", "冷却中")
    assert b["seq"] == a["seq"] + 1
    items = alerts.recent(since=base)
    assert [x["kind"] for x in items] == ["captcha", "risk"]
    assert items[0]["level"] == "error"


def test_recent_filters_kind_and_limit() -> None:
    alerts.publish("captcha", "qwen", "1")
    alerts.publish("risk", "qwen", "2")
    alerts.publish("captcha", "qwen", "3")
    assert [x["message"] for x in alerts.recent(limit=2, kind="captcha")] == ["1", "3"]


def test_subscriber_receives_and_unsubscribe() -> None:
    q = alerts.subscribe()
    try:
        alerts.publish("captcha", "qwen", "x")
        assert q.get_nowait()["message"] == "x"
    finally:
        alerts.unsubscribe(q)
    assert alerts.subscriber_count() == 0
    # 取消订阅后再发也不会报错
    alerts.publish("captcha", "qwen", "y")


def test_endpoint_requires_key_and_returns_incremental() -> None:
    client = TestClient(_app("secret"))
    assert client.get("/admin/alerts").status_code == 401
    # SSE 口令走 ?token=，空 token 直接 401（不会建立长连接）
    assert client.get("/admin/alerts/stream").status_code == 401

    base = alerts.latest_seq()
    alerts.publish("login", "qwen", "登录失效", level="error")
    r = client.get("/admin/alerts", headers={"Authorization": "Bearer secret"}, params={"since": base})
    assert r.status_code == 200
    body = r.json()
    assert body["latest"] >= base + 1
    assert [x["kind"] for x in body["items"]] == ["login"]


async def test_sse_stream_pushes_new_alert() -> None:
    """SSE 生成器：先发 hello，再实时转发一条 publish 的告警。

    不用 ASGITransport：它会把整个响应体缓冲完，无限流永远等不到第一行。
    """
    settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)

    class _Req:
        app = SimpleNamespace(state=SimpleNamespace(settings=settings))
        headers: dict = {}

        async def is_disconnected(self) -> bool:
            return False

    resp = await stream_alerts(_Req(), token="", since=-1)  # type: ignore[arg-type]
    it = resp.body_iterator
    try:
        hello = json.loads((await it.__anext__()).removeprefix("data: ").strip())
        assert hello["kind"] == "hello"

        alerts.publish("captcha", "qwen", "stream-ok", level="error")
        item = json.loads((await it.__anext__()).removeprefix("data: ").strip())
        assert item["kind"] == "captcha"
        assert item["message"] == "stream-ok"
    finally:
        await it.aclose()
    assert alerts.subscriber_count() == 0  # 关流时必须取消订阅
