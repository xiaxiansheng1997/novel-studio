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
from app.core.errors import ModelNotFound, ProviderError
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


@pytest.mark.asyncio
async def test_upstream_error_names_the_provider(monkeypatch):
    """审查 m8：报错抬头用条目 key，不用硬编码类名——openai 与 ollama
    的上游报错要在日志里分得清是谁家的。"""
    err = json.dumps({"error": "quota"})  # error 也可能直接是字符串，一样得接住
    adapter = make_adapter(make_cfg(key="ollama"))
    patch_client(
        adapter,
        lambda request: httpx.Response(200, content=f"data: {err}\n\n".encode("utf-8")),
    )
    req = ChatCompletionRequest(model="ollama-x", messages=[{"role": "user", "content": "hi"}], stream=True)
    with pytest.raises(ProviderError, match=r"\[ollama\] 上游报错") as ei:
        await drain(adapter.stream(req))
    assert "openai_compat" not in str(ei.value)


def test_parse_chunk_tolerates_non_object_json():
    """审查 m6：合法 JSON 但不是对象（null/数字/裸数组）跳行而不是 AttributeError 穿透。"""
    adapter = make_adapter(make_cfg())
    for payload in ("null", "123", '"x"', "[]"):
        assert adapter._parse_chunk(payload) is None
    assert adapter._parse_chunk("不是 JSON") is None


@pytest.mark.asyncio
async def test_finish_reason_ends_stream_without_done_marker():
    """审查 m7：上游不写 [DONE] 时，finish_reason 到手就该收——不然带长连接
    要把一次调用堵满 read=300s。"""
    body = (
        "data: " + chunk(content="好") + "\n\n"
        + "data: " + chunk(finish="stop") + "\n\n"
        # 故意不写 data: [DONE]，也不结尾空行
    )
    adapter = make_adapter(make_cfg())
    patch_client(adapter, lambda request: httpx.Response(200, content=body.encode("utf-8")))
    req = ChatCompletionRequest(model="ollama-x", messages=[{"role": "user", "content": "hi"}], stream=True)
    evs = await drain(adapter.stream(req))
    assert [ev.content for ev in evs] == ["好", None]
    assert evs[-1].finish_reason == "stop"


def test_ready_checks_config_not_liveness():
    """审查 M2：就绪 = base_url 填了 + 声明的 key 变量取得到 + 有模型；
    服务活着没不在这里查（那要发网络请求）。"""
    assert make_adapter(make_cfg()).ready() is True
    assert make_adapter(make_cfg(base_url="")).ready() is False
    assert make_adapter(make_cfg(models=[])).ready() is False
    cfg = make_cfg(api_key_env="W2A_TEST_ABSENT_KEY")
    assert make_adapter(cfg).ready() is False
    import os
    os.environ["W2A_TEST_ABSENT_KEY"] = "sk-有"
    try:
        assert make_adapter(cfg).ready() is True
    finally:
        os.environ.pop("W2A_TEST_ABSENT_KEY", None)


# ---------- 采样参数透传与 JSON 模式降级 ----------


@pytest.mark.asyncio
async def test_body_carries_sampling_stop_and_json_mode():
    """P0 的接线：上层给了就要发出去，尤其是 stop——以前只在上层做本地
    软截断，上游照样把整段生成完，白花 token。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse_body(chunk('{"a":1}', finish="stop")))

    adapter = make_adapter(make_cfg())
    patch_client(adapter, handler)
    req = ChatCompletionRequest(
        model="ollama-qwen2.5:14b",
        messages=[{"role": "user", "content": "拆一下"}],
        stream=True, temperature=0.2, max_tokens=800, stop=["END"],
        response_format={"type": "json_object"},
    )
    await drain(adapter.stream(req))

    body = seen["body"]
    assert (body["temperature"], body["max_tokens"]) == (0.2, 800)
    assert body["stop"] == ["END"]
    assert body["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_body_stays_minimal_when_no_sampling_given():
    """没给的字段不塞：有些兼容端点对多余键很敏感，不能为了「完整」把请求体撑肥。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse_body(chunk("ok", finish="stop")))

    adapter = make_adapter(make_cfg())
    patch_client(adapter, handler)
    req = ChatCompletionRequest(
        model="ollama-qwen2.5:14b", messages=[{"role": "user", "content": "hi"}], stream=True)
    await drain(adapter.stream(req))
    assert set(seen["body"]) == {"model", "messages", "stream"}


@pytest.mark.asyncio
async def test_json_mode_falls_back_when_upstream_rejects_it():
    """老端点会因为 response_format 直接 400：去掉它再发一次，而不是让整章
    拆解吃一次失败重试（那要等 30s 退避，还要把重试次数烧光）。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "response_format" in body:
            err = json.dumps({"error": {"message": "Unsupported parameter: 'response_format'"}})
            return httpx.Response(400, content=err.encode("utf-8"))
        return httpx.Response(200, content=sse_body(chunk('{"a":1}', finish="stop")))

    adapter = make_adapter(make_cfg())
    patch_client(adapter, handler)
    req = ChatCompletionRequest(
        model="ollama-qwen2.5:14b", messages=[{"role": "user", "content": "拆"}],
        stream=True, response_format={"type": "json_object"})
    evs = await drain(adapter.stream(req))

    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert "".join(ev.content or "" for ev in evs) == '{"a":1}'


@pytest.mark.asyncio
async def test_other_4xx_is_not_mistaken_for_json_mode_reject():
    """模型名写错也会 400：这种错不能被「去掉格式字段再试」掩盖，
    否则用户永远看不到「模型不存在」这句真话。"""
    hits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        err = json.dumps({"error": {"message": "The model `ollama-ghost` does not exist"}})
        return httpx.Response(400, content=err.encode("utf-8"))

    adapter = make_adapter(make_cfg())
    patch_client(adapter, handler)
    req = ChatCompletionRequest(
        model="ollama-qwen2.5:14b", messages=[{"role": "user", "content": "hi"}],
        stream=True, response_format={"type": "json_object"})
    with pytest.raises(ProviderError, match="HTTP 400"):
        await drain(adapter.stream(req))
    assert len(hits) == 1, "不是格式问题就不该重发"


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


def test_pick_model_skips_unready_compat(monkeypatch):
    """审查 M2：enabled=true 不等于就绪——OPENAI_API_KEY 没设时，
    默认模型不该被拐进一条 401 死路，而应落回反代。"""
    monkeypatch.delenv("W2A_TEST_MISSING_KEY", raising=False)
    monkeypatch.setattr("app.core.registry._adapter_classes", lambda: {"qwen": FakeProxy})
    cfg = AppConfig.from_raw({
        "providers": {
            "qwen": {"model_prefix": "qwen-", "models": ["qwen-max"]},
            "openai": {
                "compat": True, "base_url": "https://api.openai.com/v1",
                "api_key_env": "W2A_TEST_MISSING_KEY", "model_prefix": "gpt-",
                "models": ["gpt-4o-mini"],
            },
        }
    })
    ctx = AppContext(config=AppConfig(), sessions=None, store=None)
    reg = Registry(cfg, ctx)
    assert isinstance(reg.adapters["openai"], OpenAICompatAdapter)
    assert pick_model(reg, None) == "qwen-max"


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


def test_empty_prefix_does_not_hijack_routing(monkeypatch):
    """审查 m9：model_prefix 默认空串，startswith("") 恒真——忘配前缀的
    compat 条目会劫持全部模型路由。空前缀时只认自家 key 与声明过的模型 id；
    compat 条目常不给 id 加前缀（gpt-4o-mini 就直接是上游名），这条得通。"""
    monkeypatch.setattr("app.core.registry._adapter_classes", lambda: {"qwen": FakeProxy})
    cfg = AppConfig.from_raw({
        "providers": {
            "qwen": {"model_prefix": "qwen-", "models": ["qwen-max"]},
            "openai": {"compat": True, "base_url": "https://api.openai.com/v1",
                       "models": ["gpt-4o-mini"]},
        }
    })
    ctx = AppContext(config=AppConfig(), sessions=None, store=None)
    reg = Registry(cfg, ctx)
    assert reg.route("qwen-max").key == "qwen"          # 不再被空前缀劫持
    with pytest.raises(ModelNotFound):
        reg.route("gpt-5")                              # 没声明过的 id：谁都不接
    with pytest.raises(ModelNotFound):
        reg.route("nope-4o")
    assert reg.route("gpt-4o-mini").key == "openai"     # 声明过的 id 能路由
    assert reg.route("openai").key == "openai"          # 无前缀时 key 本身可路由
