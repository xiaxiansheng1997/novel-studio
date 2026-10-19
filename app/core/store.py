"""会话存储：把 OpenAI 侧的 conversation_id 映射到各家内部 session/conversation id。

各家网页端多轮依赖自己的会话 id；我们把「客户端 conversation_id + provider」映射到
内部 id，从而在无状态 HTTP 之外支持延续上下文。库文件放在 storage/ 下。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    conv_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    internal_id TEXT,
    updated_at REAL NOT NULL
);
"""


class ConversationStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @staticmethod
    def _key(conversation_id: str, provider: str) -> str:
        return f"{provider}::{conversation_id}"

    async def get(self, conversation_id: str, provider: str) -> Optional[str]:
        assert self._db is not None, "store 未初始化"
        async with self._lock:
            cur = await self._db.execute(
                "SELECT internal_id FROM sessions WHERE conv_key = ?",
                (self._key(conversation_id, provider),),
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def set(self, conversation_id: str, provider: str, internal_id: str) -> None:
        assert self._db is not None, "store 未初始化"
        import time

        async with self._lock:
            await self._db.execute(
                "INSERT INTO sessions(conv_key, provider, internal_id, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(conv_key) DO UPDATE SET "
                "internal_id=excluded.internal_id, updated_at=excluded.updated_at",
                (self._key(conversation_id, provider), provider, internal_id, time.time()),
            )
            await self._db.commit()

    async def delete(self, conversation_id: str, provider: str) -> None:
        assert self._db is not None, "store 未初始化"
        async with self._lock:
            await self._db.execute(
                "DELETE FROM sessions WHERE conv_key = ?",
                (self._key(conversation_id, provider),),
            )
            await self._db.commit()
