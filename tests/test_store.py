"""会话存储：get_messages 取最近 N 条（时间正序）、思考内容往返。"""
from __future__ import annotations

import pytest

from app.core.store import ConversationStore


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "test.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def test_get_messages_returns_latest_in_order(store: ConversationStore) -> None:
    conv = await store.create_conversation("test-model")
    for i in range(10):
        await store.add_message(conv["id"], "user", f"m{i}")

    got = await store.get_messages(conv["id"], limit=4)
    # 只取最近 4 条，且仍按时间正序（首屏就是最新内容）
    assert [m["content"] for m in got] == ["m6", "m7", "m8", "m9"]


async def test_get_messages_all_when_limit_gt_count(store: ConversationStore) -> None:
    conv = await store.create_conversation("test-model")
    await store.add_message(conv["id"], "user", "第一条")
    await store.add_message(conv["id"], "assistant", "第二条")

    got = await store.get_messages(conv["id"], limit=200)
    assert [m["content"] for m in got] == ["第一条", "第二条"]


async def test_reasoning_content_roundtrip(store: ConversationStore) -> None:
    conv = await store.create_conversation("test-model")
    await store.add_message(conv["id"], "assistant", "答案", reasoning_content="推理过程")

    got = await store.get_messages(conv["id"])
    assert got[0]["content"] == "答案"
    assert got[0]["reasoning_content"] == "推理过程"


async def test_add_message_requires_existing_conversation(store: ConversationStore) -> None:
    with pytest.raises(ValueError):
        await store.add_message("not-exist", "user", "hi")
