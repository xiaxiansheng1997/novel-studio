"""`stop` / `max_tokens` 的软截断。

网页端没有这两个参数，只能在增量流上做「本地截断」：命中 stop 串或达到字符预算就
停止下发，并告诉调用方已截断。站点侧的生成不会立刻停（那需要点页面上的停止按钮），
所以这里只保证**客户端看到的正文**符合请求。

`StreamCut` 会扣住末尾 `len(stop)-1` 个字符，避免 stop 串正好被切成两块时漏判；
流结束时调用 `flush()` 把扣住的部分补发。
"""
from __future__ import annotations

from typing import Iterable, Optional

# 网页端拿不到真实 token 计数，按约 4 字符 = 1 token 估算
CHARS_PER_TOKEN = 4


def normalize_stops(stop) -> list[str]:
    if not stop:
        return []
    if isinstance(stop, str):
        return [stop] if stop else []
    return [s for s in stop if isinstance(s, str) and s]


def char_budget(max_tokens: Optional[int]) -> int:
    if not max_tokens or max_tokens <= 0:
        return 0
    return int(max_tokens) * CHARS_PER_TOKEN


class StreamCut:
    """在增量流上做 stop / max_tokens 截断。"""

    def __init__(self, stops: Iterable[str] = (), max_tokens: Optional[int] = None):
        self.stops = [s for s in stops if s]
        self.budget = char_budget(max_tokens)
        # 扣住最长 stop 串长度 - 1 个字符，够判断跨块命中
        self.hold = max((len(s) for s in self.stops), default=0) - 1
        self.text = ""
        self.emitted = 0
        self.done = False
        self.reason = ""

    def feed(self, delta: str) -> str:
        """喂入一段增量，返回本次可以下发的文本（可能为空）。"""
        if self.done or not delta:
            return ""
        self.text += delta
        cut = self._find_stop()
        if cut is not None:
            self.done = True
            self.reason = "stop"
            return self._take(cut)
        if self.budget and len(self.text) >= self.budget:
            self.done = True
            self.reason = "max_tokens"
            return self._take(self.budget)
        keep_until = len(self.text) - max(self.hold, 0)
        return self._take(keep_until)

    def flush(self) -> str:
        """流结束：把扣住的尾巴补发（已截断则什么都不补）。"""
        if self.done:
            return ""
        return self._take(len(self.text))

    def _take(self, until: int) -> str:
        if until <= self.emitted:
            return ""
        out = self.text[self.emitted:until]
        self.emitted = until
        return out

    def _find_stop(self) -> Optional[int]:
        hit: Optional[int] = None
        for s in self.stops:
            idx = self.text.find(s, self.emitted)
            if idx >= 0 and (hit is None or idx < hit):
                hit = idx
        return hit


def cut_text(text: str, stops: Iterable[str] = (), max_tokens: Optional[int] = None) -> tuple[str, str]:
    """非流式用：一次性截断，返回 (文本, 截断原因)。"""
    if not text:
        return text, ""
    for s in stops:
        idx = text.find(s)
        if idx >= 0:
            return text[:idx], "stop"
    budget = char_budget(max_tokens)
    if budget and len(text) > budget:
        return text[:budget], "max_tokens"
    return text, ""
