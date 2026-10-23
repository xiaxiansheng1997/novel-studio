"""告警事件总线：验证码 / 风控冷却 / 登录失效等「需要人介入」的事件。

设计要点：
- 纯内存（进程重启即清空），保留最近 50 条，供 SSE 补推与 /admin/alerts 排查；
- 订阅者是 asyncio.Queue：队列满说明前端卡住，丢最旧一条再塞新的，
  绝不允许告警把请求协程反压住；
- 所有 publish 都发生在事件循环内（Playwright 操作），不需要额外加锁。
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any

# 每个订阅者最多积压这么多条，再多就丢最旧
SUB_QUEUE_MAX = 64
# 保留最近多少条供补推/查询
KEEP = 50

_recent: deque[dict[str, Any]] = deque(maxlen=KEEP)
_subscribers: set[asyncio.Queue] = set()
_seq = itertools.count(1)


def publish(kind: str, provider: str = "", message: str = "", level: str = "warn") -> dict[str, Any]:
    """广播一条告警。kind 建议：captcha / risk / login / provider。"""
    item = {
        "seq": next(_seq),
        "kind": kind,
        "provider": provider,
        "message": message,
        "level": level,
        "ts": time.time(),
    }
    _recent.append(item)
    for q in list(_subscribers):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(item)
            except Exception:  # noqa: BLE001 - 订阅者刚被移除，忽略
                pass
    return item


def latest_seq() -> int:
    return _recent[-1]["seq"] if _recent else 0


def recent(since: int = 0, limit: int = 20, kind: str = "") -> list[dict[str, Any]]:
    """取 seq > since 的告警，返回最后 limit 条（顺序保留）。"""
    items = [x for x in _recent if x["seq"] > since and (not kind or x["kind"] == kind)]
    return items[-limit:] if limit > 0 else items


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=SUB_QUEUE_MAX)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def subscriber_count() -> int:
    return len(_subscribers)
