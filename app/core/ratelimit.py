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
    """组合：每分钟限流 + 并发信号量 + 最小请求间隔。每个 provider 一个实例。

    min_interval_sec：相邻两次请求的最小间隔，**从上一次回复结束算起**。
    上游风控（如千问 RGV587）对"刚答完就立刻发下一条"最敏感，所以用结束
    时刻而非开始时刻计时；0 表示不限制。
    """

    def __init__(
        self,
        key: str,
        per_minute: int,
        max_concurrency: int,
        min_interval_sec: float = 0.0,
    ):
        self.key = key
        self.per_minute = max(1, per_minute)
        self.max_concurrency = max(1, max_concurrency)
        self._limiter = SlidingWindowLimiter(per_minute)
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self.min_interval = max(0.0, float(min_interval_sec))
        # 下一次允许开始请求的时间点（monotonic）
        self._next_start = 0.0
        # 多个请求同时到达时按顺序放行，避免它们一起冲破间隔
        self._turn = asyncio.Lock()

    def check_rate(self) -> None:
        try:
            self._limiter.acquire()
        except RateLimited:
            raise RateLimited(self.key, 60)

    async def wait_turn(self) -> float:
        """等到允许开始请求；返回实际等待秒数。"""
        if self.min_interval <= 0:
            return 0.0
        async with self._turn:
            wait = self._next_start - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_start = time.monotonic() + self.min_interval
            return max(0.0, wait)

    async def __aenter__(self) -> "ProviderThrottle":
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self._sem.release()
        if self.min_interval > 0:
            # 间隔从上一次回复结束重新计时
            self._next_start = max(self._next_start, time.monotonic() + self.min_interval)
