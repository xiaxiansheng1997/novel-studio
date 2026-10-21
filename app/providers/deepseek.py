"""DeepSeek 网页端适配器（浏览器捕获路径）。

要点：
- DeepSeek 对话接口有 PoW + AWS WAF，本项目**不实现 PoW**：在真实登录页面里驱动发送，
  让前端自算 PoW，我们旁路 tee 走对话接口的 SSE 响应。
- 网页端 SSE 不是 OpenAI 格式，而是「快照 + 增量」几种形态混着来（实测录制）：
    event: update
    data: {"v":{"response":{"content":"","fragments":[{"type":"RESPONSE","content":"光合"}]}}}
    data: {"p":"response/fragments/-1/content","o":"APPEND","v":"作用是"}
    data: {"v":"植物"}          ← 主流的逐字增量，v 是字符串
  另外标题事件的 data 是 {"content":"会话标题"}——它不是正文，
  混进来就会出现「回答只有一个标题」。
- 思考链（THINK fragment / response/thinking_content）走 reasoning_content。
- 官方封号严：务必在 config.yaml 保持 max_concurrency=1 且低频使用。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from loguru import logger

from app.core.sse import DeltaEvent
from app.providers._browser_base import BrowserStreamProvider
from app.providers._replay import generic_extract_text

# 兼容多种思考链标签：<thought>  <think>  <thinking>  <reasoning>
_THINK_RE = re.compile(r"<(thought|think|thinking|reasoning)>(.*?)</\1>", re.S)

# 只带这些键的载荷是会话标题/状态事件，不是回答正文
_META_KEYS = {"content", "title", "chat_session_id", "request_message_id", "response_message_id"}


def _last_fragment(resp: dict, want: str) -> str:
    """取最后一条指定类型 fragment 的 content（随流不断覆盖成最新全文）。"""
    frags = resp.get("fragments")
    if not isinstance(frags, list):
        return ""
    out = ""
    for frag in frags:
        if not isinstance(frag, dict):
            continue
        if (frag.get("type") or "").upper() != want:
            continue
        c = frag.get("content")
        if isinstance(c, str) and c:
            out = c
    return out


class DeepSeekAdapter(BrowserStreamProvider):
    def url_hint(self) -> str:
        return "completion"

    def input_selectors(self) -> list[str]:
        return ["textarea#chat-input", "textarea", "div[contenteditable='true']"]

    def build_prompt(self, req) -> str:
        # 每次请求重新开始：思考链去重游标必须归零
        self._think = ""
        return super().build_prompt(req)

    # ---------- 解析 ----------
    def parse_payload(self, payload: str) -> Optional[DeltaEvent]:
        obj = json.loads(payload)
        if not isinstance(obj, dict):
            return None

        # 1) OpenAI 风格（兼容直连/旧版）
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

        # 2) JSON-Patch 增量：{"p":"response/fragments/-1/content","o":"APPEND","v":"…"}
        path = obj.get("p")
        if isinstance(path, str) and path:
            return self._patch_event(path, obj.get("o"), obj.get("v"))

        # 3) 快照：{"v":{"response":{...,"fragments":[...]}}}
        v = obj.get("v")
        if isinstance(v, dict):
            resp = v.get("response")
            if isinstance(resp, dict):
                return self._snapshot_event(resp)
        # 4) 逐字增量：{"v":"植物"}（v 是纯字符串，没有 p/o）
        if isinstance(v, str) and v:
            return DeltaEvent(content=v)

        # 5) event: title / 状态位：只有元信息键，没有正文
        if set(obj.keys()) <= _META_KEYS:
            logger.debug(f"[deepseek] 跳过标题/状态事件: {payload[:60]}")
            return None

        # 6) 泛用兜底（老版本或结构变化时还能出字）
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

    def _patch_event(self, path: str, op: Any, val: Any) -> Optional[DeltaEvent]:
        low = path.lower()
        append = str(op or "").upper() == "APPEND"

        if isinstance(val, list):
            # 批量补丁：{"p":"response/fragments","o":"APPEND","v":[{...}]}
            answers: list[str] = []
            thinks: list[str] = []
            for it in val:
                if not isinstance(it, dict):
                    continue
                c = it.get("content")
                if not isinstance(c, str) or not c:
                    continue
                (thinks if (it.get("type") or "").upper() == "THINK" else answers).append(c)
            if answers:
                return DeltaEvent(content="".join(answers))
            if thinks:
                return self._thinking("".join(thinks), append)
            return None

        if not isinstance(val, str) or not val:
            return None
        if "thinking" in low or low.endswith("/reasoning_content"):
            return self._thinking(val, append)
        if low == "content" or low.endswith("/content"):
            return DeltaEvent(content=val)
        return None

    def _snapshot_event(self, resp: dict) -> Optional[DeltaEvent]:
        answer = _last_fragment(resp, "RESPONSE")
        think = _last_fragment(resp, "THINK")
        if not answer:
            c = resp.get("content")
            if isinstance(c, str) and c:
                answer = c
        if answer:
            return DeltaEvent(content=answer)  # 正文开始后思考链不再重复下发
        if think:
            return self._thinking(think, append=False)
        return None

    def _thinking(self, text: str, append: bool) -> Optional[DeltaEvent]:
        """思考链去重：快照是全文覆盖，补丁是增量追加。"""
        prev = getattr(self, "_think", "")
        if append:
            delta = text
            self._think = prev + text
        elif text.startswith(prev):
            delta = text[len(prev):]
            self._think = text
        else:
            delta = text
            self._think = prev + text
        return DeltaEvent(reasoning_content=delta) if delta else None
