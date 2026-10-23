"""请求 trace id：响应头回写、日志前缀、按 rid 过滤。

这三件事一起构成“出问题能对上号”的最小闭环：
调用方拿到 X-W2A-Request-Id -> 日志里每行都有 [rid] -> /admin/logs?rid= 能筛出来。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.request_log import (
    CURRENT_RID,
    LOG_BUFFER,
    RequestLogMiddleware,
    get_logs,
    log_patcher,
    new_rid,
)


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.get("/v1/ping")
    async def ping():  # noqa: ANN202 - 测试用最小端点
        return {"ok": True}

    @app.get("/other")
    async def other():  # noqa: ANN202
        return {"ok": True}

    return TestClient(app)


def test_response_carries_request_id() -> None:
    LOG_BUFFER.clear()
    r = _client().get("/v1/ping")
    assert r.status_code == 200
    rid = r.headers.get("X-W2A-Request-Id")
    assert rid and len(rid) == 12
    # 同一请求会落到缓冲里，便于 /admin/logs 反查
    assert LOG_BUFFER[-1].rid == rid


def test_incoming_request_id_is_reused() -> None:
    LOG_BUFFER.clear()
    r = _client().get("/v1/ping", headers={"X-W2A-Request-Id": "abc123def456"})
    assert r.headers["X-W2A-Request-Id"] == "abc123def456"
    assert LOG_BUFFER[-1].rid == "abc123def456"


def test_non_api_paths_are_not_logged() -> None:
    LOG_BUFFER.clear()
    _client().get("/other")
    assert not LOG_BUFFER


def test_log_patcher_prefixes_message() -> None:
    token = CURRENT_RID.set("r1")
    try:
        record = {"message": "hello"}
        log_patcher(record)
        assert record["message"] == "[r1] hello"
    finally:
        CURRENT_RID.reset(token)
    # 不在请求上下文里就原样保留
    record = {"message": "hello"}
    log_patcher(record)
    assert record["message"] == "hello"


def test_get_logs_filters_by_rid() -> None:
    LOG_BUFFER.clear()
    _client().get("/v1/ping", headers={"X-W2A-Request-Id": "aaa111"})
    _client().get("/v1/ping", headers={"X-W2A-Request-Id": "bbb222"})
    rows = get_logs(rid="aaa")
    assert [r["rid"] for r in rows] == ["aaa111"]
    assert len(get_logs()) == 2


def test_new_rid_is_short_and_unique() -> None:
    ids = {new_rid() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 12 for i in ids)
