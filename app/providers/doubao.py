"""豆包网页端适配器（浏览器捕获路径）。

要点：
- 豆包对话依赖前端动态生成的 X-Signature / X-Device-Id，纯 HTTP 重放不可持续，
  因此走浏览器捕获：在真实页面里驱动发送，旁路 tee 走对话流响应，签名由前端自算。
- 响应结构随版本变化较大，这里用 OpenAI 风格优先 + 泛用取文兜底；
  若解析不到文本，请对照 fixtures/doubao.json 的 response_sample 微调 parse_payload。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger

from app.api.schemas import ChatCompletionRequest
from app.core.sse import DeltaEvent
from app.providers._browser_base import BrowserStreamProvider
from app.providers._replay import generic_extract_text


def _deep_text(obj: Any) -> str:
    """递归取第一段非空文本：豆包正文常藏在 content.text_block.text 里。"""
    if isinstance(obj, dict):
        for k in ("text", "content"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v
        for v in obj.values():
            t = _deep_text(v)
            if t:
                return t
    elif isinstance(obj, list):
        for it in obj:
            t = _deep_text(it)
            if t:
                return t
    return ""


def _block_text(text: str) -> Optional[str]:
    """解析消息块 `[{"block_type":10000,"content":{"text_block":{"text":"…"}}}]`，返回其中正文。

    不是消息块结构时返回 None（调用方按普通增量处理）。
    """
    s = (text or "").strip()
    if not s or s[0] not in "[{" or "block_type" not in s:
        return None
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    items = obj if isinstance(obj, list) else [obj]
    blocks = [it for it in items if isinstance(it, dict) and "block_type" in it]
    if not blocks:
        return None
    for it in blocks:
        t = _deep_text(it)
        if t:
            return t
    return ""


def _norm(text: str) -> str:
    return "".join((text or "").split())


def _strip_echo(blob: str, prompt: str) -> str:
    """块里若把提问本身带上了（用户消息回显），把开头那段提问剥掉。

    豆包有时会把「提问 + 回答开头」拼在同一个块里，整块丢掉就会少几个字。
    """
    s = (blob or "").strip()
    p = (prompt or "").strip()
    if p and s.startswith(p):
        tail = s[len(p):]
        # 只有提问后面跟着空白/标点才算“回显+正文”；否则可能是回答本身
        # 以提问原文开头（如复述题目），不能剥。
        if not tail or tail[0] in " \n\r\t:：,，。;；":
            return tail.lstrip(" \n\r\t:：,，。;；")
    return s


class DoubaoAdapter(BrowserStreamProvider):
    def url_hint(self) -> str:
        # 豆包对话相关端点常见关键字
        return "completion"

    def input_selectors(self) -> list[str]:
        return ["textarea[placeholder]", "textarea", "div[contenteditable='true']"]

    def build_prompt(self, req: ChatCompletionRequest) -> str:
        prompt = super().build_prompt(req)
        # 供 parse_payload 判断“这一块是不是用户消息回显”
        self._prompt = (prompt or "").strip()
        return prompt

    def parse_payload(self, payload: str) -> Optional[DeltaEvent]:
        obj = json.loads(payload)

        # 用户消息回显（role=user）不是回答
        msg = obj.get("message") if isinstance(obj, dict) else None
        if isinstance(msg, dict) and msg.get("role") == "user":
            return None

        # OpenAI 风格
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0] or {}
            delta = c0.get("delta") or c0.get("message") or {}
            if delta.get("content"):
                return DeltaEvent(content=delta["content"], finish_reason=c0.get("finish_reason"))

        # 豆包常见：{"data": {...}} 或 {"payload": ...}
        text = generic_extract_text(obj)
        if not text:
            return None
        # 消息块结构：用户那条（正文就是本轮提问）要丢；助手首块同样是块结构，
        # 但正文是回答开头，无脑过滤会表现为“回答少几个字”
        blob = _block_text(text)
        if blob is not None:
            prompt = getattr(self, "_prompt", "")
            rest = _strip_echo(blob, prompt)
            if not _norm(rest) or (prompt and _norm(rest) == _norm(prompt)):
                logger.debug(f"[doubao] 跳过消息块回显: {blob[:40]!r}")
                return None
            return DeltaEvent(content=rest)
        return DeltaEvent(content=text)
