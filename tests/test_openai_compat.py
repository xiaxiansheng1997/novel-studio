"""迭代 C：OpenAI 兼容直连通道（官方 API / Ollama）的回归测试。

覆盖三层：适配器本身（SSE 解析/前缀剥离/鉴权/报错）、registry 登记与路由、
pick_model 的「compat 优先、反代 fallback」顺序。全部走 httpx MockTransport，
不碰网络。
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
import pytest

from app.api.schemas import ChatCompletionRequest
from app.config import AppConfig, ProviderConfig
from app.core.base import AppContext, ProviderAdapter
from app.core.errors import ProviderError
from app.core.registry import Registry
from app.llm.gateway import pick_model
from app.providers.openai_compat import OpenAICompatAdapter


def make_cfg(**kw) -> ProviderConfig:
    base = dict(
        key="ollama",
        compat=True,
        base_url="http://localhost:11434/v1",
        api_key_env="",
        model_prefix="ollama-",
        strip_prefix=True,
        models=[{"id": "ollama-qwen2.5:14b"}],
    )
    base.update(kw)
    return ProviderConfig(**base)


def make_adapter(cfg: ProviderConfig) -> OpenAICompatAdapter:
    ctx = AppContext(config=AppConfig(), sessions=None, store=None)
    return OpenAICompatAdapter(cfg, ctx)


def sse_body(*payloads: str) -> bytes:
    """按 OpenAI 的样子拼 SSE：每帧一个 data: 行，中间夹空行，结尾 [DONE]。"""
    parts = []
    for p in payloads:
        parts.append(f"data: {p}\n\n")
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode("utf-8")


def chunk(content=None, reasoning=None, finish=None) -> str:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return json.dumps(
        {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]},
        ensure_ascii=False,
    )


def patch_client(adapter: OpenAICompatAdapter, handler) -> None:
    transport = httpx.MockTransport(handler)
    adapter._make_client = lambda: httpx.AsyncClient(transport=transport)  # type: ignore[method-assign]


async def drain(aiter: AsyncIterator) -> list:
    return [ev async for ev in aiter]


@pytest.mark.asyncio
async def test_stream_parses_openai_sse_and_strips_prefix():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=sse_body(
                chunk(reasoning="想"),
                ": keep-alive",           # 注释行要静默跳过
                chunk(reasoning="一下"),
                chunk(content="灯下"),
                chunk(content="有三人"),
                chunk(finish="stop"),
            ),
        )

    adapter = make_adapter(make_cfg())
    patch_client(adapter, handler)
    req = ChatCompletionRequest(
        model="ollama-qwen2.5:14b",
        messages=[{"role": "system", "content": "你是作家"}, {"role": "user", "content": "写一段"}],
        stream=True,
    )
    evs = await drain(adapter.stream(req))

    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["auth"] is None  # 没配 api_key_env 就不带鉴权头
    # strip_prefix：ollama-qwen2.5:14b → qwen2.5:14b
    assert seen["body"]["model"] == "qwen2.5:14b"
    assert seen["body"]["stream"] is True
    roles = [m["role"] for m in seen["body"]["messages"]]
    assert roles == ["system", "user"]
    text = "".join(ev.content or "" for ev in evs)
    reason = "".join(ev.reasoning_content or "" for ev in evs)
    assert (text, reason) == ("灯下有三人", "想一下")
    assert evs[-1].finish_reason == "stop"
    assert not any(ev.hint for ev in evs)  # 直连通道没有「把提示当正文」的坑


@pytest.mark.asyncio
async def test_openai_no_strip_and_bearer_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse_body(chunk(content="ok")))

    cfg = make_cfg(
        key="openai", base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
        model_prefix="gpt-", strip_prefix=False, models=[{"id": "gpt-4o-mini"}],
    )
    adapter = make_adapter(cfg)
    patch_client(adapter, handler)
    req = ChatCompletionRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], stream=True)
    evs = await drain(adapter.stream(req))

    assert seen["auth"] == "Bearer sk-test-123"
    assert seen["body"]["model"] == "gpt-4o-mini"  # 官方 API：id 本身就是请求名
    assert evs[0].content == "ok"


@pytest.mark.asyncio
async def test_missing_key_is_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    adapter = make_adapter(make_cfg(key="openai", api_key_env="OPENAI_API_KEY"))
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        await adapter.ensure_session()
    # stream 也要在发请求前拦住，不能拿空 key 打出去再吃 401
    req = ChatCompletionRequest(model="openai-x", messages=[{"role": "user", "content": "hi"}], stream=True)
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        await drain(adapter.stream(req))


@pytest.mark.asyncio
async def test_no_base_url_and_http_error_and_stream_error():
    adapter = make_adapter(make_cfg(base_url=""))
    req = ChatCompletionRequest(model="ollama-x", messages=[{"role": "user", "content": "hi"}], stream=True)
    with pytest.raises(ProviderError, match="base_url"):
        await drain(adapter.stream(req))

    adapter = make_adapter(make_cfg())
    patch_client(adapter, lambda request: httpx.Response(429, content=b"rate limited"))
    with pytest.raises(ProviderError, match="429"):
        await drain(adapter.stream(req))

    err = json.dumps({"error": {"message": "model not found"}})
    patch_client(
        adapter,
        lambda request: httpx.Response(200, content=f"data: {err}\n\n".encode("utf-8")),
    )
    with pytest.raises(ProviderError, match="model not found"):
        await drain(adapter.stream(req))


# ---------- registry 登记与路由 ----------


class FakeProxy(ProviderAdapter):
    """顶替反代适配器：只验证 registry 的选择逻辑，不真的开浏览器。"""

    def matches(self, model: str) -> bool:
        return model.startswith(self.cfg.model_prefix) or model == self.cfg.key

    async def ensure_session(self) -> None:
        pass

    async def stream(self, req) -> AsyncIterator:
        yield None  # pragma: no cover


def mixed_config() -> AppConfig:
    return AppConfig.from_raw({
        "providers": {
            "qwen": {"model_prefix": "qwen-", "models": ["qwen-max"]},
            "ollama": {
                "compat": True, "base_url": "http://x/v1", "model_prefix": "ollama-",
                "strip_prefix": True, "models": ["ollama-qwen2.5:14b"],
                "price_out_per_1k_tokens": 0.001,
            },
        }
    })


def make_registry(monkeypatch) -> Registry:
    monkeypatch.setattr("app.core.registry._adapter_classes", lambda: {"qwen": FakeProxy})
    ctx = AppContext(config=AppConfig(), sessions=None, store=None)
    return Registry(mixed_config(), ctx)


def test_registry_registers_compat_and_routes_by_prefix(monkeypatch):
    reg = make_registry(monkeypatch)
    assert isinstance(reg.adapters["ollama"], OpenAICompatAdapter)
    assert isinstance(reg.get("qwen"), FakeProxy)
    assert reg.route("ollama-qwen2.5:14b").key == "ollama"
    assert reg.route("qwen-max").key == "qwen"
    # 单价字段要能从 yaml 一路带到适配器上（成本预估吃它）
    assert reg.adapters["ollama"].cfg.price_out_per_1k_tokens == 0.001


def test_apply_config_rebuilds_when_class_flips(monkeypatch):
    reg = make_registry(monkeypatch)
    old = reg.adapters["ollama"]
    # ollama 从 compat 翻回普通（key 不在反代类表）→ 该被移除而不是拿旧机制跑新配置
    cfg = mixed_config()
    cfg.providers["ollama"].compat = False
    result = reg.apply_config(cfg)
    assert "ollama" not in reg.adapters
    assert "ollama" in result["removed"]
    assert old is not reg.adapters.get("ollama")
    # 翻回 compat → 重新登记
    cfg2 = mixed_config()
    reg.apply_config(cfg2)
    assert isinstance(reg.adapters["ollama"], OpenAICompatAdapter)


# ---------- pick_model：compat 优先，反代 fallback ----------


def test_pick_model_prefers_enabled_compat(monkeypatch):
    reg = make_registry(monkeypatch)
    # compat 排在 deepseek（原默认主力）之前：反代降为 fallback
    assert pick_model(reg, None) == "ollama-qwen2.5:14b"
    # 显式指定的模型仍然最大
    assert pick_model(reg, "qwen-max") == "qwen-max"
    # 指定了不存在的模型 → 落到 compat 而不是抛错
    assert pick_model(reg, "nope-9") == "ollama-qwen2.5:14b"


def test_pick_model_falls_back_without_compat(monkeypatch):
    monkeypatch.setattr("app.core.registry._adapter_classes", lambda: {"qwen": FakeProxy})
    cfg = AppConfig.from_raw({"providers": {"qwen": {"model_prefix": "qwen-", "models": ["qwen-max"]}}})
    ctx = AppContext(config=AppConfig(), sessions=None, store=None)
    reg = Registry(cfg, ctx)
    # 没配 compat、deepseek 也不在路由表：任意兜底，不能崩
    assert pick_model(reg, None) == "qwen-max"


def test_gateway_resolve_contract_untouched(monkeypatch):
    # 写作队列拿 pick_model 的结果进 stream：compat 优先后不该出现「路由不到」
    reg = make_registry(monkeypatch)
    model = pick_model(reg, None)
    assert reg.route(model).cfg.compat is True
