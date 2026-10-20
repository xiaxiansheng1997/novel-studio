"""识别上游的固定拒绝卡片（内容审核 / 风控 / 未登录游客态兜底）。

这类响应不是模型输出，而是站点策略层直接回的固定文案；千问的 mime_type
通常是 multi_load/iframe（一段卡片渲染指令，不是文本增量）。如果当成正常
回答返回，调用方完全分不清“模型不想答”和“环境/账号被拦”，排查成本极高。
"""
from __future__ import annotations

import re

# 已确认的兜底文案片段：去掉空白后做子串匹配，容忍标点/空格差异。
# 只收“换话题”这类站点特有的固定话术，避免把模型正常拒绝也误判。
BLOCKED_PHRASES: tuple[str, ...] = (
    "换个话题聊聊",
    "换一个话题聊聊",
    "我们换一个话题",
)

# 承载卡片渲染的 mime_type 特征（正常文本增量不会出现）
CARD_MIME_HINTS: tuple[str, ...] = ("multi_load", "iframe")


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def looks_blocked(text: str) -> bool:
    """文本是否命中固定拒绝文案。"""
    if not text:
        return False
    body = compact(text)
    return any(p in body for p in BLOCKED_PHRASES)


def is_card_mime(mime: str) -> bool:
    """mime_type 是否表示一张渲染卡片而非文本增量。"""
    low = (mime or "").lower()
    return any(h in low for h in CARD_MIME_HINTS)
