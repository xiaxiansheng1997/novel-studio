"""Provider 通用工具：fixture 读取、提示词抽取、SSE 增量切分、请求头注入、模板填充、泛用取文。"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.api.schemas import ChatCompletionRequest
from app.config import BASE_DIR

FIXTURES_DIR = BASE_DIR / "fixtures"

# recon 抓取时约定用户发送的样例文本，用于在 body 模板里定位要替换的字段
RECON_SAMPLE_PROMPT = "你好"


def load_fixture(key: str) -> Optional[dict[str, Any]]:
    p = FIXTURES_DIR / f"{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def extract_text(content: Any) -> str:
    """把 OpenAI message.content（str 或多模态数组）抽成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return str(content)


def last_user_text(req: ChatCompletionRequest) -> str:
    for m in reversed(req.messages):
        if m.role == "user":
            return extract_text(m.content)
    return ""


def render_prompt(req: ChatCompletionRequest) -> str:
    """把 system + 历史轮次 + 最后一条用户消息合成单个 prompt。

    网页端多数为单次发送或自有会话；这里以"整体携带上下文"的方式保证多轮语义，
    避免依赖各家不稳定的 conversation_id。
    """
    sys_parts = [extract_text(m.content) for m in req.messages if m.role == "system"]
    convo: list[str] = []
    for m in req.messages:
        if m.role == "user":
            convo.append(f"用户：{extract_text(m.content)}")
        elif m.role == "assistant":
            convo.append(f"助手：{extract_text(m.content)}")
    # 最后一条用户消息放最前，确保模型聚焦
    body = "\n".join(convo)
    head = "\n".join(sys_parts)
    if head:
        return f"{head}\n\n{body}"
    return body


class SSEBuffer:
    """跨网络块安全地切分 SSE：累积缓冲，产出完整的 data 载荷字符串。"""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        payloads: list[str] = []
        while True:
            idx = self._buf.find("\n")
            if idx < 0:
                break
            line = self._buf[:idx].strip()
            self._buf = self._buf[idx + 1 :]
            if not line:
                continue
            payloads.extend(self._parse_line(line))
        return payloads

    def flush(self) -> list[str]:
        tail = self._buf.strip()
        self._buf = ""
        return self._parse_line(tail) if tail else []

    @staticmethod
    def _parse_line(line: str) -> list[str]:
        if line.startswith("data:"):
            data = line[len("data:"):].strip()
            return [data] if data else []
        if line.startswith(("event:", "id:", "retry:", ":")):
            return []
        # 兼容裸 JSON / NDJSON 流
        if line.startswith("{") or line.startswith("["):
            return [line]
        return []


def is_done(payload: str) -> bool:
    return payload.strip() in ("[DONE]", "done", "[done]")


def inject_headers(headers: dict[str, str], runtime: dict[str, str]) -> dict[str, str]:
    """把 {{RUNTIME_INJECT:<name>}} 占位符替换为实时值（authorization/cookie 等）。

    runtime 用「小写头名 -> 值」提供；缺失的敏感头会被丢弃而非留占位符。
    """
    out: dict[str, str] = {}
    for k, v in headers.items():
        if isinstance(v, str) and v.startswith("{{RUNTIME_INJECT:"):
            rv = runtime.get(k.lower())
            if rv:
                out[k] = rv
            continue
        out[k] = v
    return out


def fill_body(body: Any, real_prompt: str) -> Any:
    """把 body 模板里等于 recon 样例文本（"你好"）的字符串替换为真实 prompt。"""
    if isinstance(body, str):
        return real_prompt if body.strip() == RECON_SAMPLE_PROMPT else body
    if isinstance(body, list):
        return [fill_body(x, real_prompt) for x in body]
    if isinstance(body, dict):
        return {k: fill_body(v, real_prompt) for k, v in body.items()}
    return body


_TEXT_KEYS_PRIORITY = ["content", "text", "delta", "answer", "message", "reply", "output", "result", "response"]


def generic_extract_text(obj: Any) -> str:
    """从任意 JSON 结构里尽力抽取"助手增量文本"。provider 专属解析失败时的兜底。"""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        for it in obj:
            t = generic_extract_text(it)
            if t:
                return t
        return ""
    if isinstance(obj, dict):
        # OpenAI 风格优先
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            c0 = choices[0]
            for key in ("delta", "message"):
                if isinstance(c0.get(key), dict) and c0[key].get("content"):
                    return str(c0[key]["content"])
        for key in _TEXT_KEYS_PRIORITY:
            if key in obj:
                t = generic_extract_text(obj[key])
                if t:
                    return t
        return ""
    return ""
