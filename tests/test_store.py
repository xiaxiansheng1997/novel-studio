"""会话存储：消息顺序/思考内容/回退与搜索/站点会话映射。"""
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


async def test_session_mapping_roundtrip_and_cleanup(store: ConversationStore) -> None:
    conv = await store.create_conversation("test-model")
    await store.set(conv["id"], "deepseek", "site-1")
    assert await store.get(conv["id"], "deepseek") == "site-1"

    # 同一个 API 会话换了站点会话：覆盖而不是插新行
    await store.set(conv["id"], "deepseek", "site-2")
    assert await store.get(conv["id"], "deepseek") == "site-2"

    # 删会话要连映射一起清，否则重建会话可能拿到旧站点会话
    await store.delete_conversation(conv["id"])
    assert await store.get(conv["id"], "deepseek") is None


async def test_rewind_drops_only_trailing_answer(store: ConversationStore) -> None:
    conv = await store.create_conversation("test-model")
    u1 = await store.add_message(conv["id"], "user", "q1")
    await store.add_message(conv["id"], "assistant", "a1")

    assert (await store.last_message(conv["id"]))["content"] == "a1"
    # 重新生成：只删尾部助手回复，提问保留
    assert await store.delete_trailing_assistant(conv["id"]) == 1
    assert (await store.last_message(conv["id"]))["content"] == "q1"
    assert await store.delete_trailing_assistant(conv["id"]) == 0

    # 编辑重发：从指定消息（含）删到末尾
    assert await store.delete_from_message(conv["id"], u1["id"]) == 1
    assert await store.get_messages(conv["id"]) == []


async def test_delete_from_unknown_message_deletes_nothing(store: ConversationStore) -> None:
    conv = await store.create_conversation("test-model")
    await store.add_message(conv["id"], "user", "q1")
    await store.add_message(conv["id"], "assistant", "a1")

    # 拿错 id 不能把会话删空（曾用 COALESCE(...,-1) 出过这个事故）
    assert await store.delete_from_message(conv["id"], "missing-id") == 0
    assert len(await store.get_messages(conv["id"])) == 2


async def test_search_messages_snippet_and_like_escape(store: ConversationStore) -> None:
    conv = await store.create_conversation("test-model", title="测试会话")
    await store.add_message(conv["id"], "user", "如何配置 100% 的速率_限制")

    hits = await store.search_messages("100%")
    assert len(hits) == 1
    assert hits[0]["conversation_id"] == conv["id"]
    assert "100%" in hits[0]["snippet"] and hits[0]["title"] == "测试会话"

    # % / _ 必须转义：否则会变成通配符命中所有消息
    assert await store.search_messages("不存在%") == []
    assert await store.search_messages("x_y") == []
