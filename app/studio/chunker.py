"""切块：把正文切成 500-900 字的检索单元。

为什么要切而不是整章塞：检索要能给「出处 + 局部上下文」，整章 3000 字塞进去
既浪费预算又稀释相似度；而按固定字符硬切会把一句话劈成两半，召回后读起来断裂。

策略：
1. 先按空行分段（写作时自然形成的段落边界）
2. 攒到目标字数就切一刀
3. 单段超长（比如整篇没换行的粘贴稿）时，退到句号级别再切
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 目标长度：参考 bge-small 的 512 token 上限（中文约 1 字 1 token），
# 900 字左右刚好不超；也接近「一节自然段」的体量。
TARGET_CHARS = 700
MAX_CHARS = 1000
MIN_CHARS = 120          # 太短的尾块并到前一块，避免产生大量噪声块
SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])")


@dataclass
class Chunk:
    seq: int
    start: int
    end: int
    text: str


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """按空行分段，返回 [(start, end)]，保留原文偏移。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    for block in text.split("\n"):
        start = pos
        end = pos + len(block)
        if block.strip():
            spans.append((start, end))
        pos = end + 1  # 跳过换行符
    return spans


def _split_long(text: str, start: int) -> list[Chunk]:
    """超长单段按句子切，句子仍超长就硬切。"""
    out: list[Chunk] = []
    buf = ""
    buf_start = start
    for sent in SENT_SPLIT.split(text):
        if not sent:
            continue
        if len(buf) + len(sent) > MAX_CHARS and buf:
            out.append(Chunk(len(out), buf_start, buf_start + len(buf), buf))
            buf_start += len(buf)
            buf = ""
        # 单句本身就超长：硬切
        while len(sent) > MAX_CHARS:
            out.append(Chunk(len(out), buf_start, buf_start + MAX_CHARS, sent[:MAX_CHARS]))
            buf_start += MAX_CHARS
            sent = sent[MAX_CHARS:]
        buf += sent
    if buf.strip():
        out.append(Chunk(len(out), buf_start, buf_start + len(buf), buf))
    return out


def split_text(text: str, *, target: int = TARGET_CHARS, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """把一段正文切成若干块，保持原文顺序与字符偏移。"""
    if not text or not text.strip():
        return []

    chunks: list[Chunk] = []
    buf = ""
    buf_start = 0

    for start, end in _paragraph_spans(text):
        para = text[start:end]
        if len(para) > max_chars:
            # 先把已攒的吐出来，再单独处理这一段
            if buf.strip():
                chunks.append(Chunk(len(chunks), buf_start, buf_start + len(buf), buf))
                buf = ""
            chunks.extend(_split_long(para, start))
            continue

        if not buf:
            buf_start = start
        buf += ("\n" if buf else "") + para
        if len(buf) >= target:
            chunks.append(Chunk(len(chunks), buf_start, buf_start + len(buf), buf))
            buf = ""

    if buf.strip():
        chunks.append(Chunk(len(chunks), buf_start, buf_start + len(buf), buf))

    # 尾块太短就并进前一块（不超上限时）
    if len(chunks) >= 2 and len(chunks[-1].text) < MIN_CHARS:
        tail = chunks.pop()
        prev = chunks[-1]
        merged = prev.text + "\n" + tail.text
        if len(merged) <= max_chars:
            chunks[-1] = Chunk(prev.seq, prev.start, tail.end, merged)

    # 重排 seq，保证 0..n-1 连续
    return [Chunk(i, c.start, c.end, c.text.strip()) for i, c in enumerate(chunks) if c.text.strip()]
