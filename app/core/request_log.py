"""请求日志中间件：记录每个 /v1、/admin 请求的元信息，并给每个请求发一个 trace id。

- `X-W2A-Request-Id`：调用方带同名头则沿用，否则现生成；回写在响应头与 `/admin/logs`。
- 日志上下文：中间件把 rid 放进 ContextVar，`log_patcher` 给每条 loguru 日志加 `[rid]`
  前缀，这样「一次请求的所有日志」能直接 grep 到一起（含 provider 内部告警）。
- 缓冲仍是最近 500 条；落盘另由 main.py 的 loguru 文件 sink 负责（按天轮转）。
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

# 当前请求的 trace id；不在请求里时为空串
CURRENT_RID: ContextVar[str] = ContextVar("w2a_rid", default="")

_RID_HEADER = "X-W2A-Request-Id"


def new_rid() -> str:
    return uuid.uuid4().hex[:12]


def log_patcher(record: dict) -> None:
    """给请求内的日志加 `[rid]` 前缀（不改全局 format，兼容任意 sink）。"""
    rid = CURRENT_RID.get()
    if rid:
        record["message"] = f"[{rid}] {record['message']}"


@dataclass
class RequestLog:
    ts: float = 0.0
    rid: str = ""
    method: str = ""
    path: str = ""
    model: str = ""
    provider: str = ""
    status: int = 0
    duration_ms: float = 0.0
    stream: bool = False
    error: Optional[str] = None


# 环形缓冲（最近 500 条）
LOG_BUFFER: deque[RequestLog] = deque(maxlen=500)


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        path = request.url.path

        # Only log API paths
        if not path.startswith(("/v1/", "/admin/")):
            return await call_next(request)

        rid = (request.headers.get(_RID_HEADER) or "").strip()[:64] or new_rid()
        request.state.rid = rid
        token = CURRENT_RID.set(rid)

        # Try to extract model from request body (for chat completions)
        model = ""
        stream = False
        if path == "/v1/chat/completions" and request.method == "POST":
            try:
                body = await request.body()
                import json
                j = json.loads(body)
                model = j.get("model", "")
                stream = j.get("stream", False)
            except Exception:
                pass

        try:
            response = await call_next(request)
        finally:
            CURRENT_RID.reset(token)
        duration = (time.perf_counter() - start) * 1000

        response.headers[_RID_HEADER] = rid
        entry = RequestLog(
            ts=time.time(),
            rid=rid,
            method=request.method,
            path=path,
            model=model,
            provider=model.split("-")[0] if "-" in model else "",
            status=response.status_code,
            duration_ms=round(duration, 1),
            stream=stream,
        )
        LOG_BUFFER.append(entry)
        return response


def get_logs(limit: int = 50, rid: Optional[str] = None) -> list[dict]:
    source = LOG_BUFFER if not rid else [e for e in LOG_BUFFER if rid in e.rid]
    items = list(source)[-limit:]
    return [asdict(e) for e in reversed(items)]


def get_stats() -> dict:
    if not LOG_BUFFER:
        return {"total": 0, "avg_ms": 0, "error_rate": 0, "by_provider": {}}
    items = list(LOG_BUFFER)
    total = len(items)
    errors = sum(1 for e in items if e.status >= 400)
    durations = [e.duration_ms for e in items if e.duration_ms > 0]
    avg_ms = sum(durations) / len(durations) if durations else 0

    by_provider: dict[str, int] = {}
    for e in items:
        if e.provider:
            by_provider[e.provider] = by_provider.get(e.provider, 0) + 1

    return {
        "total": total,
        "errors": errors,
        "error_rate": round(errors / total * 100, 1) if total else 0,
        "avg_ms": round(avg_ms, 0),
        "by_provider": by_provider,
    }
