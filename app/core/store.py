"""会话存储：对话持久化 + 消息历史 + provider session 映射。

表结构：
- conversations: 对话元信息（标题、模型、provider、系统提示词）
- messages: 每条消息（role、content、reasoning_content）
- sessions: provider 内部 id 映射（兼容旧逻辑）
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    conv_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    internal_id TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT '新对话',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    system_prompt TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    reasoning_content TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at DESC);
"""


class ConversationStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ==================== Provider session mapping (legacy) ====================

    @staticmethod
    def _key(conversation_id: str, provider: str) -> str:
        return f"{provider}::{conversation_id}"

    async def get(self, conversation_id: str, provider: str) -> Optional[str]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT internal_id FROM sessions WHERE conv_key = ?",
                (self._key(conversation_id, provider),),
            )
            row = await cur.fetchone()
            return row[0] if row else None

    async def set(self, conversation_id: str, provider: str, internal_id: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "INSERT INTO sessions(conv_key, provider, internal_id, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(conv_key) DO UPDATE SET "
                "internal_id=excluded.internal_id, updated_at=excluded.updated_at",
                (self._key(conversation_id, provider), provider, internal_id, time.time()),
            )
            await self._db.commit()

    async def delete_session(self, conversation_id: str, provider: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "DELETE FROM sessions WHERE conv_key = ?",
                (self._key(conversation_id, provider),),
            )
            await self._db.commit()

    # ==================== Conversations CRUD ====================

    async def create_conversation(self, model: str, title: str = "新对话", provider: str = "", system_prompt: str = "") -> dict[str, Any]:
        assert self._db is not None
        conv_id = uuid.uuid4().hex[:16]
        now = time.time()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO conversations(id, title, provider, model, system_prompt, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (conv_id, title, provider, model, system_prompt or None, now, now),
            )
            await self._db.commit()
        return {"id": conv_id, "title": title, "provider": provider, "model": model, "system_prompt": system_prompt, "created_at": now, "updated_at": now}

    async def list_conversations(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, title, provider, model, system_prompt, created_at, updated_at "
                "FROM conversations ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_conversation(self, conv_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, title, provider, model, system_prompt, created_at, updated_at "
                "FROM conversations WHERE id = ?",
                (conv_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_conversation(self, conv_id: str, *, title: Optional[str] = None, system_prompt: Optional[str] = None) -> bool:
        assert self._db is not None
        updates = []
        params = []
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if system_prompt is not None:
            updates.append("system_prompt = ?")
            params.append(system_prompt)
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(time.time())
        params.append(conv_id)
        async with self._lock:
            await self._db.execute(
                f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await self._db.commit()
        return True

    async def delete_conversation(self, conv_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            await self._db.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            await self._db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            await self._db.commit()
        return True

    # ==================== Messages ====================

    async def add_message(self, conv_id: str, role: str, content: str = "", reasoning_content: str = "") -> dict[str, Any]:
        assert self._db is not None
        msg_id = uuid.uuid4().hex[:16]
        now = time.time()
        async with self._lock:
            # SQLite 默认不开 PRAGMA foreign_keys，这里显式校验，避免写入永远查不到的孤儿消息
            cur = await self._db.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,))
            if await cur.fetchone() is None:
                raise ValueError(f"会话不存在: {conv_id}")
            await self._db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, reasoning_content, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (msg_id, conv_id, role, content, reasoning_content or '', now),
            )
            # Update conversation's updated_at (and auto-title from first user message)
            await self._db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conv_id),
            )
            await self._db.commit()
        return {"id": msg_id, "conversation_id": conv_id, "role": role, "content": content, "reasoning_content": reasoning_content, "created_at": now}

    async def get_messages(self, conv_id: str, limit: int = 200) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, role, content, reasoning_content, created_at "
                "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?",
                (conv_id, limit),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def auto_title(self, conv_id: str, first_message: str) -> None:
        """用第一条用户消息的前 20 字自动生成标题。"""
        assert self._db is not None
        title = first_message[:20].replace('\n', ' ').strip()
        if not title:
            title = "新对话"
        async with self._lock:
            await self._db.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND title = '新对话'",
                (title, time.time(), conv_id),
            )
            await self._db.commit()
