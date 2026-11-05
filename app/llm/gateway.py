"""LLM 网关：把写作流程与具体 provider 解耦。

M0 实测（docs/m0-baseline.md）：deepseek 在 28k 输入、140 字/秒、JSON 合法率 100%，
因此默认主力走 deepseek；其它 provider 保留为手动切换或后续降级用。

M1 只实现 Web2API 通道（复用现有 registry/adapter）。
接官方 API 时新增一个实现即可，写作层不用改。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from loguru import logger

from app.api.schemas import ChatCompletionRequest
from app.core.errors import ProviderError
from app.core.registry import Registry

# M0 实测定下的默认主力；可在请求里用 model 覆盖
DEFAULT_MODEL = "deepseek-chat"


@dataclass
class GenChunk:
    """一次生成的中途增量。"""

    text: str = ""
    reasoning: str = ""


@dataclass
class GenResult:
    text: str
    model: str
    provider: str
    prompt_chars: int
    elapsed_ms: int


class Gateway:
    def __init__(self, registry: Registry):
        self.reg = registry

    def resolve(self, model: str) -> tuple[str, str]:
        """返回 (provider_key, model)。模型名不认识时回落到默认主力。"""
        try:
            adapter = self.reg.route(model)
        except Exception:  # noqa: BLE001 - 模型名写错不该让整个写作流程挂掉
            logger.warning(f"[studio] 模型 {model} 未路由到任何 provider，回落到 {DEFAULT_MODEL}")
            adapter = self.reg.route(DEFAULT_MODEL)
            model = DEFAULT_MODEL
        return adapter.key, model

    async def stream(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        mode: str = "quick",
        history_mode: str = "prompt",
        thinking: bool = False,
        search: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> AsyncIterator[GenChunk]:
        """驱动一次生成，逐块产出正文增量。

        history_mode 固定 prompt：写作场景每次都要干净上下文，
        站点会话里的历史会和装配好的上下文叠两遍。

        temperature / max_tokens / json_mode 是能力而非要求：只有直连通道用得上，
        反代适配器不读这几个字段（网页端没有这些开关）。以前这里一个采样参数
        都没有，等于把「写正文」和「抽 JSON」一起交给上游默认温度——官方 API
        默认 1.0 会让拆解输出乱飘，本地小模型默认值又常把正文写成流水账。
        """
        adapter = self.reg.route(model)
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        req = ChatCompletionRequest(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            history_mode="prompt",
            mode=mode,
            thinking=thinking,
            search=search,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"} if json_mode else None,
        )
        throttle = self.reg.throttle(adapter.key)
        await throttle.wait_turn()
        async with throttle:
            async for ev in adapter.stream(req):
                # provider 在验证码/风控/页面异常时会把「⚠️ …」当增量下发，
                # 那是给聊天用户看的提示，不是模型回答。写进正文就是 40 字的废稿，
                # 所以写作层直接报错，交给队列重试或暂停。
                if ev.hint:
                    raise ProviderError(adapter.key, ev.content or "上游返回了错误提示")
                if ev.content or ev.reasoning_content:
                    yield GenChunk(text=ev.content or "", reasoning=ev.reasoning_content or "")

    async def complete(self, **kwargs) -> GenResult:
        """非流式：收齐再返回，给摘要/抽取这类不需要实时展示的任务用。"""
        model = kwargs.get("model") or DEFAULT_MODEL
        started = time.perf_counter()
        parts: list[str] = []
        async for chunk in self.stream(**kwargs):
            if chunk.text:
                parts.append(chunk.text)
        text = "".join(parts)
        provider, _ = self.resolve(model)
        return GenResult(
            text=text,
            model=model,
            provider=provider,
            prompt_chars=len(kwargs.get("system", "")) + len(kwargs.get("prompt", "")),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def pick_model(registry: Registry, preferred: Optional[str] = None) -> str:
    """选一个当前可路由的模型：优先指定值，其次直连通道，再次 deepseek，最后任意一个。

    直连（compat）优先是迭代 C 的口径：官方 API / Ollama 没有风控也没有滑块，
    挂批量最稳；网页端反代降为 fallback——配了照旧能用，只是默认不先选它。
    只挑已启用且已就绪的 compat：enabled=false 的示范条目、base_url 没填、
    api_key_env 指了个取不到值的变量，都不该把默认模型拐进必死通道（审查 M2）。
    """
    candidates = [preferred]
    for adapter in registry.adapters.values():
        if not (getattr(adapter.cfg, "compat", False) and adapter.cfg.models):
            continue
        ready = getattr(adapter, "ready", None)
        if callable(ready) and not ready():
            continue
        candidates.append(adapter.cfg.models[0].id)
        break
    candidates.append(DEFAULT_MODEL)
    for name in candidates:
        if not name:
            continue
        try:
            registry.route(name)
            return name
        except Exception:  # noqa: BLE001
            continue
    for adapter in registry.adapters.values():
        models = [m.id for m in adapter.cfg.models]
        if models:
            return models[0]
    return DEFAULT_MODEL
