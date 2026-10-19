"""DeepSeek 网页端适配器（浏览器捕获路径）。

要点：
- DeepSeek 对话接口有 PoW + AWS WAF，本项目**不实现 PoW**：在真实登录页面里驱动发送，
  让前端自算 PoW，我们旁路 tee 走对话接口的 SSE 响应。
- 深度思考内容（若出现在正文里）用正则剥离到 reasoning_content。
- 官方封号严：务必在 config.yaml 保持 max_concurrency=1 且低频使用。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from app.core.sse import DeltaEvent
from app.providers._browser_base import BrowserStreamProvider
from app.providers._replay import generic_extract_text

# 兼容多种思考链标签：<thought>  <think>  <thinking>  <reasoning>
_THINK_RE = re.compile(r"<(thought|think|thinking|reasoning)>(.*?)</\1>", re.S)


class DeepSeekAdapter(BrowserStreamProvider):
    def url_hint(self) -> str:
        return "completion"

    def input_selectors(self) -> list[str]:
        return ["textarea#chat-input", "textarea", "div[contenteditable='true']"]

    def parse_payload(self, payload: str) -> Optional[DeltaEvent]:
        obj = json.loads(payload)
        # 优先 OpenAI 风格
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0] or {}
            delta = c0.get("delta") or c0.get("message") or {}
            content = delta.get("content") or ""
            reasoning = delta.get("reasoning_content") or ""
            finish = c0.get("finish_reason")
            if content or reasoning or finish:
                return DeltaEvent(
                    content=content or None,
                    reasoning_content=reasoning or None,
                    finish_reason=finish,
                )
        text = generic_extract_text(obj)
        if not text:
            return None
        reasoning_parts: list[str] = []

        def _strip(m: re.Match) -> str:
            reasoning_parts.append(m.group(2))
            return ""

        content = _THINK_RE.sub(_strip, text)
        return DeltaEvent(
            content=content or None,
            reasoning_content=("".join(reasoning_parts) or None),
        )
