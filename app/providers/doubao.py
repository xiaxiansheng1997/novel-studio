"""豆包网页端适配器（浏览器捕获路径）。

要点：
- 豆包对话依赖前端动态生成的 X-Signature / X-Device-Id，纯 HTTP 重放不可持续，
  因此走浏览器捕获：在真实页面里驱动发送，旁路 tee 走对话流响应，签名由前端自算。
- 响应结构随版本变化较大，这里用 OpenAI 风格优先 + 泛用取文兜底；
  若解析不到文本，请对照 fixtures/doubao.json 的 response_sample 微调 parse_payload。
"""
from __future__ import annotations

import json
from typing import Optional

from app.core.sse import DeltaEvent
from app.providers._browser_base import BrowserStreamProvider
from app.providers._replay import generic_extract_text


class DoubaoAdapter(BrowserStreamProvider):
    def url_hint(self) -> str:
        # 豆包对话相关端点常见关键字
        return "completion"

    def input_selectors(self) -> list[str]:
        return ["textarea[placeholder]", "textarea", "div[contenteditable='true']"]

    def parse_payload(self, payload: str) -> Optional[DeltaEvent]:
        obj = json.loads(payload)

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
        return DeltaEvent(content=text)
