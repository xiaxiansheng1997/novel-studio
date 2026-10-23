"""OpenAI Chat Completions 兼容的请求/响应模型（Pydantic v2）。

只覆盖对话反代常用字段；各家不支持的字段（temperature/top_p 等）接受但透传或忽略。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    # content 可为纯文本，也可为 OpenAI 多模态数组（text/image_url/file...）
    content: Union[str, list[dict[str, Any]], None] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    # 以下为透传/可选字段，provider 视能力支持
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Union[str, list[str]]] = None
    # 显式声明，避免「传了却静默丢弃」；具体处理见 openai_routes.handlers
    # n>1 直接 400；stop/max_tokens 本地软截断；tools 本轮只告警不执行
    n: Optional[int] = None
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Union[str, dict[str, Any]]] = None
    stream_options: Optional[dict[str, Any]] = None
    # 自定义：延续同一会话（可选），以及 DeepSeek/豆包的思考/联网开关
    conversation_id: Optional[str] = Field(default=None, alias="conversation_id")
    thinking: Optional[bool] = None
    search: Optional[bool] = None
    # 模式预设（quick/think/search/expert）：与模型名后缀是双轨，参数优先
    mode: Optional[str] = None
    # 重新生成：先丢弃该会话末尾的助手回复，再按同样的用户消息重跑
    # （前端把最后一条用户消息再发一遍；服务端不会重复落库同一句）
    regenerate: bool = False
    # 多轮策略覆盖：site = 用站点自己的会话；prompt = 每轮自己拼历史（不传则用 provider 配置）
    history_mode: Optional[Literal["site", "prompt"]] = None
    # OpenAI o 系列风格：medium/high 视为要深度思考
    reasoning_effort: Optional[str] = None
    # OpenAI 联网搜索开关，等价 search=true
    web_search_options: Optional[dict[str, Any]] = None

    model_config = {"extra": "allow"}


class ChatCompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: Optional[str] = None
    reasoning_content: Optional[str] = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    index: int = 0
    message: ChatCompletionMessage
    finish_reason: Optional[str] = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:24]}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class Delta(BaseModel):
    role: Optional[Literal["assistant"]] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: Delta
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[StreamChoice]

    def sse_data(self) -> str:
        """序列化成 SSE 的 data: 行内容。"""
        return self.model_dump_json(exclude_none=True)


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "web2api"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


def gen_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"
