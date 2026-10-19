"""限流与并发控制：每个 provider 一个滑动窗口限流 + 并发信号量。

DeepSeek/豆包 封号风险高，必须把并发压到很低；令牌桶按每分钟请求数限制，
超限抛 429（带 Retry-After），提示客户端指数退避。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque

from app.core.errors import W2AError


class RateLimited(W2AError):
    def __init__(self, provider: str, retry_after: int):
        super().__init__(
            f"[{provider}] 触发本地限流，请 {retry_after}s 后重试（请勿高频刷量以免被封号）",
            type_="rate_limit_error",
            code="rate_limit_exceeded",
            status=429,
        )
        self.retry_after = retry_after


class SlidingWindowLimiter:
    """依赖无关的每分钟滑动窗口限流器。"""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self._events: Deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        window = 60.0
        while self._events and now - self._events[0] > window:
            self._events.popleft()
        if len(self._events) >= self.per_minute:
            retry_after = int(window - (now - self._events[0])) + 1
            raise RateLimited("global", max(1, retry_after))
        self._events.append(now)


class ProviderThrottle:
    """组合：每分钟限流 + 并发信号量。每个 provider 一个实例。"""

    def __init__(self, key: str, per_minute: int, max_concurrency: int):
        self.key = key
        self._limiter = SlidingWindowLimiter(per_minute)
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    def check_rate(self) -> None:
        try:
            self._limiter.acquire()
        except RateLimited:
            raise RateLimited(self.key, 60)

    async def __aenter__(self) -> "ProviderThrottle":
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self._sem.release()
