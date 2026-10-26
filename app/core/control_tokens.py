"""剥离上游正文里夹带的站点 UI 占位标记。

千问等站点会把「追问建议 / 内联按钮 / 文档卡片」这类前端渲染指令以 `[[inline_action_reply_1]]`、
`[[doc_common_card_1]]` 的形式混在文本增量里下发，网页端会把它替换成可点击的 chip 或卡片，
但 API 调用方只会看到一串方括号垃圾。这类标记不是模型正文，必须在下发前剔除。

难点是标记可能被流式分块截断（例如 `[[inline_action_` + `reply_1]]`），所以用带状态
的过滤器：完整标记直接删，疑似半个标记暂扣等下一块，流结束时结算归还。
正文与思考内容各用一个过滤器，两边互不干扰。
"""
from __future__ import annotations

import re
from typing import AsyncIterator, List

from loguru import logger

from app.core.sse import DeltaEvent

# 完整控制标记：[[some_word-1]]
CONTROL_TOKEN_RE = re.compile(r"\[\[[A-Za-z0-9_.\-]{1,64}\]\]")
# 站点卡片/建议类占位符（[[doc_common_card_1]]、[[inline_action_reply_1]]）：
# 容忍全角括号、内部空白与零宽字符，名字里必须带 card/reply/action/suggest
_CARD_CHAR = r"(?:[A-Za-z0-9_\-]|[ \t\u200b-\u200f])"
CARD_TOKEN_RE = re.compile(
    r"[\[［]{2}[ \t\u200b-\u200f]*"
    rf"{_CARD_CHAR}*?(?:card|reply|action|suggest){_CARD_CHAR}*?\d+"
    r"[ \t\u200b-\u200f]*[\]］]{2}",
    re.IGNORECASE,
)
# 标记名允许的最大长度，超过就不再当作半个标记（避免正常方括号内容被长期扣住）
_MAX_HOLD = 80
# 半个标记可能出现的形式：半角/全角括号 + 名字字符（不含换行，避免跨行扣住正文）
_TOKEN_TAIL_RE = re.compile(r"[ \t\u200b-\u200fA-Za-z0-9_.\-\[\]［］]*")


def _could_be_token(tail: str) -> bool:
    """以半角/全角 "[" 开头的片段，是否还可能长成一条控制标记。"""
    if not tail or len(tail) > _MAX_HOLD:
        return False
    if tail[0] not in "[［":
        return False
    return _TOKEN_TAIL_RE.fullmatch(tail) is not None


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
        self._buf = CARD_TOKEN_RE.sub(self._drop, self._buf)
        # 标记通常独占一行，删掉后会留下三四个连续换行，压平成一段
        if self.dropped:
            self._buf = re.sub(r"\n{3,}", "\n\n", self._buf)
        # 末尾若像半个标记，先扣住等下一块（标记只能从开括号或末尾单个括号开始）
        idx = max(self._buf.rfind("[["), self._buf.rfind("［［"))
        if idx == -1 and self._buf.endswith(("[", "［")):
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
    """包在 provider.stream() 外面，对整条增量流做控制标记过滤。

    正文与思考内容各用一个过滤器（两者的缓冲不能混，否则半截标记会互相污染）。
    """
    clean = ControlTokenFilter()
    clean_reason = ControlTokenFilter()

    async for ev in events:
        content = clean.feed(ev.content) if ev.content else ""
        reasoning = clean_reason.feed(ev.reasoning_content) if ev.reasoning_content else ""
        if ev.finish_reason:
            tail_c = clean.flush()
            tail_r = clean_reason.flush()
            if content or reasoning or tail_c or tail_r:
                yield DeltaEvent(
                    content=(content + tail_c) or None,
                    reasoning_content=(reasoning + tail_r) or None,
                    hint=ev.hint,
                )
            yield DeltaEvent(content=None, finish_reason=ev.finish_reason)
            continue
        if content or reasoning:
            yield DeltaEvent(content=content or None, reasoning_content=reasoning or None,
                            hint=ev.hint)
    tail_c = clean.flush()
    tail_r = clean_reason.flush()
    if tail_c or tail_r:
        yield DeltaEvent(content=tail_c or None, reasoning_content=tail_r or None)
    dropped = clean.dropped + clean_reason.dropped
    if dropped:
        # 留一条日志，便于确认“上游确实发了控制标记”而不是模型自己写了方括号
        logger.info(f"已剔除上游控制标记 {len(dropped)} 处：{dropped[:3]}")
