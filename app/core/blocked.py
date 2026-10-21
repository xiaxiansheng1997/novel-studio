"""识别上游的固定拒绝卡片（内容审核 / 风控 / 未登录游客态兜底）。

这类响应不是模型输出，而是站点策略层直接回的固定文案；千问的 mime_type
通常是 multi_load/iframe（一段卡片渲染指令，不是文本增量）。如果当成正常
回答返回，调用方完全分不清“模型不想答”和“环境/账号被拦”，排查成本极高。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

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


# 上游用 HTTP 200 + SSE 事件回绝的业务错误（豆包最常见）：
#   event: STREAM_ERROR
#   data: {"error_code":710022002,"error_msg":"当前服务访问频繁，请稍后重试"}
# 这种载荷里没有正文，如果被当成“解析不出文本就跳过”，调用方只能拿到一条
# 空回答（表现为只看到页面上的“系统繁忙”），必须识别出来显式报错。
def find_upstream_error(obj: Any) -> Optional[tuple[str, str]]:
    """从一条 JSON 载荷里认出业务错误，命中则返回 (code, message)。"""
    if not isinstance(obj, dict):
        return None
    code = obj.get("error_code")
    msg = obj.get("error_msg") or obj.get("msg")
    if code not in (None, "", 0, "0") and msg:
        return str(code), str(msg)
    base = obj.get("base_resp")
    if isinstance(base, dict) and base.get("ret") not in (None, "", 0, "0") and base.get("errmsg"):
        return str(base.get("ret")), str(base.get("errmsg"))
    err = obj.get("error")
    if isinstance(err, dict) and (err.get("message") or err.get("msg")):
        return str(err.get("code") or ""), str(err.get("message") or err.get("msg"))
    return None


# 业务错误里“要你去做人机验证”的信号：豆包风控会在 extra.decision 里带
# `type:verify` / `verify_scene:doubao_message_web`，阿里则用 rgv587 / punish / baxia。
# 命中这些关键字时可以尝试自动过滑块，而不是直接当普通错误报给用户。
VERIFY_MARKERS: tuple[str, ...] = (
    "verify", "captcha", "rgv587", "punish", "baxia", "nocaptcha", "验证", "滑块",
)


def needs_human_verify(obj: Any) -> bool:
    """一条上游载荷（dict/list/字符串）里是否带着“需要人机验证”的信号。"""
    if obj is None:
        return False
    if isinstance(obj, (dict, list)):
        try:
            text = json.dumps(obj, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(obj)
    else:
        text = str(obj)
    low = text.lower()
    return any(m in low for m in VERIFY_MARKERS)
