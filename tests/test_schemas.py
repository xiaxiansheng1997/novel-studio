"""请求参数处理：n>1 报错、stop/max_tokens 软截断、tools 告警头。"""
from __future__ import annotations

import pytest

from app.api.openai_routes import _reject_unsupported, _warn_headers
from app.api.schemas import ChatCompletionRequest
from app.core.cut import StreamCut, cut_text, normalize_stops
from app.core.errors import InvalidRequest


def _req(**kw) -> ChatCompletionRequest:
    base = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return ChatCompletionRequest(**base)


def test_n_greater_than_one_rejected() -> None:
    with pytest.raises(InvalidRequest) as ei:
        _reject_unsupported(_req(n=2))
    assert ei.value.status == 400
    assert ei.value.code == "unsupported_parameter"


def test_n_equal_one_accepted() -> None:
    _reject_unsupported(_req(n=1))


def test_tools_produces_warning_header() -> None:
    req = _req(tools=[{"type": "function", "function": {"name": "f"}}])
    assert _warn_headers(req) == {"X-W2A-Warning": "tools-not-supported"}


def test_plain_request_has_no_warning_header() -> None:
    assert _warn_headers(_req()) == {}


def test_normalize_stops_accepts_str_and_list() -> None:
    assert normalize_stops("END") == ["END"]
    assert normalize_stops(["a", "", None]) == ["a"]
    assert normalize_stops(None) == []


def test_cut_text_stop() -> None:
    text, why = cut_text("你好，到此为止。后面不该出现", ["到此为止"])
    assert text == "你好，"
    assert why == "stop"


def test_cut_text_max_tokens() -> None:
    # 4 字符 ≈ 1 token
    text, why = cut_text("abcdefghij", max_tokens=1)
    assert text == "abcd"
    assert why == "max_tokens"


def test_cut_text_without_limits() -> None:
    assert cut_text("原样返回") == ("原样返回", "")


def test_stream_cut_stop_across_chunks() -> None:
    """stop 串被切在两个增量之间时也要命中，且命中前的内容正常下发。"""
    cut = StreamCut(stops=["###"])
    out = ""
    for piece in ["内容##", "#后面都丢掉"]:
        out += cut.feed(piece)
    assert cut.done
    assert cut.reason == "stop"
    assert out == "内容"


def test_stream_cut_max_tokens_and_flush() -> None:
    cut = StreamCut(max_tokens=2)  # 8 字符预算
    assert cut.feed("一二三四五六") == "一二三四五六"
    assert cut.feed("七八九十") == "七八"
    assert cut.done
    assert cut.reason == "max_tokens"
    assert cut.flush() == ""


def test_stream_cut_holds_tail_until_flush() -> None:
    cut = StreamCut(stops=["END"])  # 扣住 2 个字符
    assert cut.feed("abc") == "a"
    assert cut.flush() == "bc"
