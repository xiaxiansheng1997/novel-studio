"""请求日志中间件：记录每个 /v1/chat/completions 请求的元信息。"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


@dataclass
class RequestLog:
    ts: float = 0.0
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

        response = await call_next(request)
        duration = (time.perf_counter() - start) * 1000

        entry = RequestLog(
            ts=time.time(),
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


def get_logs(limit: int = 50) -> list[dict]:
    items = list(LOG_BUFFER)[-limit:]
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
