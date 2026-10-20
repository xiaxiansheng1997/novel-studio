"""剥离上游正文里夹带的站点 UI 占位标记。

千问等站点会把「追问建议 / 内联按钮」这类前端渲染指令以 `[[inline_action_reply_1]]`
的形式混在文本增量里下发，网页端会把它替换成可点击的 chip，但 API 调用方只会看到
一串方括号垃圾。这类标记不是模型正文，必须在下发前剔除。

难点是标记可能被流式分块截断（例如 `[[inline_action_` + `reply_1]]`），所以用带状态
的过滤器：完整标记直接删，疑似半个标记暂扣等下一块，流结束时结算归还。
"""
from __future__ import annotations

import re
from typing import AsyncIterator, List

from loguru import logger

from app.core.sse import DeltaEvent

# 完整控制标记：[[some_word-1]]
CONTROL_TOKEN_RE = re.compile(r"\[\[[A-Za-z0-9_.\-]{1,64}\]\]")
# 标记名允许的最大长度，超过就不再当作半个标记（避免正常方括号内容被长期扣住）
_MAX_HOLD = 68


def _could_be_token(tail: str) -> bool:
    """以 "[[" 开头的片段，是否还可能长成一条控制标记。"""
    if len(tail) > _MAX_HOLD:
        return False
    if tail == "[":
        return True
    if not tail.startswith("[["):
        return False
    # 允许末尾已经收到单个 "]"（"]" 可能被切到下一块）
    return re.fullmatch(r"[A-Za-z0-9_.\-]{0,64}\]?", tail[2:]) is not None


class ControlTokenFilter:
    """跨分块删除控制标记；feed() 返回可以安全下发的文本。"""

    def __init__(self) -> None:
        self._buf = ""
        self.dropped: List[str] = []

    def _drop(self, match: "re.Match[str]") -> str:
        self.dropped.append(match.group(0))
        return ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        self._buf = CONTROL_TOKEN_RE.sub(self._drop, self._buf)
        # 末尾若像半个标记，先扣住等下一块（标记只能从 "[[" 或末尾单个 "[" 开始）
        idx = self._buf.rfind("[[")
        if idx == -1 and self._buf.endswith("["):
            idx = len(self._buf) - 1
        if idx != -1 and _could_be_token(self._buf[idx:]):
            pending = self._buf[:idx]
            self._buf = self._buf[idx:]
        else:
            pending = self._buf
            self._buf = ""
        # 末尾空白也扣住：紧跟标记被删时能一并丢掉，不留空行
        stripped = pending.rstrip()
        if len(stripped) != len(pending):
            self._buf = pending[len(stripped):] + self._buf
            pending = stripped
        return pending

    def flush(self) -> str:
        """流结束：归还扣住的文本（纯空白且确实删过标记则丢弃）。"""
        tail = self._buf
        self._buf = ""
        if self.dropped and not tail.strip():
            return ""
        return tail


async def strip_control_tokens(
    events: AsyncIterator[DeltaEvent],
) -> AsyncIterator[DeltaEvent]:
    """包在 provider.stream() 外面，对整条增量流做控制标记过滤。"""
    clean = ControlTokenFilter()
    async for ev in events:
        if ev.content:
            safe = clean.feed(ev.content)
            if safe:
                yield DeltaEvent(content=safe)
        if ev.finish_reason:
            tail = clean.flush()
            if tail:
                yield DeltaEvent(content=tail)
            yield DeltaEvent(content=None, finish_reason=ev.finish_reason)
    tail = clean.flush()
    if tail:
        yield DeltaEvent(content=tail)
    if clean.dropped:
        # 留一条日志，便于确认“上游确实发了控制标记”而不是模型自己写了方括号
        logger.info(f"已剔除上游控制标记 {len(clean.dropped)} 处：{clean.dropped[:3]}")
