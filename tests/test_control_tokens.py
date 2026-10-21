"""控制标记过滤器：分块截断、全角/零宽字符、思考流、空行压平、正常方括号保留。"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Iterable

from app.core.control_tokens import ControlTokenFilter, strip_control_tokens
from app.core.sse import DeltaEvent


def _run(events: Iterable[DeltaEvent]) -> list[DeltaEvent]:
    """把一次增量流跑完，返回过滤后的所有事件。"""

    async def source() -> AsyncIterator[DeltaEvent]:
        for ev in events:
            yield ev

    async def sink() -> list[DeltaEvent]:
        return [ev async for ev in strip_control_tokens(source())]

    return asyncio.run(sink())


def _text(events: Iterable[DeltaEvent]) -> str:
    return "".join(ev.content or "" for ev in events)


def _reasoning(events: Iterable[DeltaEvent]) -> str:
    return "".join(ev.reasoning_content or "" for ev in events)


def test_complete_token_is_dropped() -> None:
    f = ControlTokenFilter()
    assert f.feed("你好[[inline_action_reply_1]]世界") == "你好世界"
    assert f.dropped == ["[[inline_action_reply_1]]"]


def test_token_split_across_chunks() -> None:
    """标记被切成两块时不能漏出半截，也不能把正文一起吞掉。"""
    f = ControlTokenFilter()
    assert f.feed("你好[[inline_action_") == "你好"
    assert f.feed("reply_1]]世界") == "世界"


def test_fullwidth_brackets_and_zero_width() -> None:
    f = ControlTokenFilter()
    out = f.feed("A［［doc_common_card_1］］B") + f.flush()
    assert out == "AB"


def test_blank_lines_flattened_after_removal() -> None:
    """标记独占一行时，删完剩三四连换行，要压回两连。"""
    f = ControlTokenFilter()
    out = f.feed("第一段\n\n[[doc_common_card_1]]\n\n第二段") + f.flush()
    assert out == "第一段\n\n第二段"


def test_normal_brackets_kept() -> None:
    """正常方括号内容（含空格、非标记词）不能被误删。"""
    f = ControlTokenFilter()
    text = "见 [[hello world]] 与 [链接](https://example.com)"
    assert f.feed(text) + f.flush() == text
    assert f.dropped == []


def test_half_token_returned_on_flush() -> None:
    """流结束时仍是半个标记，必须原样归还，不能丢正文。"""
    out = _run([DeltaEvent(content="正文[[incomplete")])
    assert _text(out) == "正文[[incomplete"


def test_pure_token_chunk_yields_nothing() -> None:
    out = _run([DeltaEvent(content="[[inline_action_reply_1]]")])
    assert out == []


def test_reasoning_and_content_filtered_separately() -> None:
    """思考流与正文各用一个过滤器，半截标记不能互相污染。"""
    out = _run(
        [
            DeltaEvent(reasoning_content="思考中[[doc_common_card_"),
            DeltaEvent(reasoning_content="1]]继续想"),
            DeltaEvent(content="答案"),
            DeltaEvent(finish_reason="stop"),
        ]
    )
    assert _reasoning(out) == "思考中继续想"
    assert _text(out) == "答案"
    assert out[-1].finish_reason == "stop"
