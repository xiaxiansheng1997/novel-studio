"""把各家 provider 的原始增量归一成 OpenAI chunk，并提供非流式聚合。

provider 适配器只需 yield DeltaEvent（content / reasoning_content / finish_reason），
由这里统一产出 ChatCompletionChunk；非流式请求则缓冲全部增量合成一条完整响应。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from app.api.schemas import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    Choice,
    ChatCompletionMessage,
    Delta,
    StreamChoice,
    Usage,
    gen_id,
)


@dataclass
class DeltaEvent:
    """provider 归一后的单个增量。"""

    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    finish_reason: Optional[str] = None


def make_chunk(chat_id: str, model: str, event: DeltaEvent, *, first: bool = False) -> ChatCompletionChunk:
    delta = Delta(
        role="assistant" if first else None,
        content=event.content,
        reasoning_content=event.reasoning_content,
    )
    return ChatCompletionChunk(
        id=chat_id,
        model=model,
        choices=[StreamChoice(index=0, delta=delta, finish_reason=event.finish_reason)],
    )


@dataclass
class Accumulator:
    """把一串 DeltaEvent 聚合成非流式响应。"""

    chat_id: str = field(default_factory=lambda: gen_id())
    model: str = ""
    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    finish_reason: Optional[str] = None

    def feed(self, ev: DeltaEvent) -> None:
        if ev.content:
            self.content.append(ev.content)
        if ev.reasoning_content:
            self.reasoning.append(ev.reasoning_content)
        if ev.finish_reason:
            self.finish_reason = ev.finish_reason

    def to_response(self) -> ChatCompletionResponse:
        text = "".join(self.content)
        reason = "".join(self.reasoning)
        # 粗略用量估算（无真实 token 计数），约 4 字符 = 1 token
        completion_tokens = max(1, len(text) // 4) if text else 0
        return ChatCompletionResponse(
            id=self.chat_id,
            model=self.model,
            choices=[
                Choice(
                    index=0,
                    message=ChatCompletionMessage(
                        content=text,
                        reasoning_content=reason or None,
                    ),
                    finish_reason=self.finish_reason or "stop",
                )
            ],
            usage=Usage(prompt_tokens=0, completion_tokens=completion_tokens, total_tokens=completion_tokens),
        )


async def collect(aiter: AsyncIterator[DeltaEvent], model: str) -> ChatCompletionResponse:
    """消费 provider 的增量流，返回完整非流式响应。"""
    acc = Accumulator(model=model)
    async for ev in aiter:
        acc.feed(ev)
    return acc.to_response()
