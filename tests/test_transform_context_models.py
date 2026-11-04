"""迭代 D-2：加工类任务的两处补强。

1. **带节内上下文**：选区孤零零交给模型，它对齐不了前后文的语气、称谓、时态。
   前端把选区前后各 800 字拼成窗口随请求发，后端塞进 <context> 且明说「只用对齐、
   不要输出它」——否则模型会把前后文一起复述，写回时变成双重替换。
2. **跨模型对比**：给了 models 就每家各出一条（接管 candidates）。同一家抽第二稿
   的差异，往往不如换一家的手感大。每家一条各记一条 runs，variant 递增。

口径都在路由层，用假网关捕获拼好的 prompt，不碰真模型。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.config import AppConfig, ProviderConfig
from app.core.registry import Registry
from app.studio.store import StudioStore


def compat_cfg(**kw) -> ProviderConfig:
    base = dict(
        key="openai", compat=True, base_url="https://api.example/v1",
        model_prefix="gpt-", models=[{"id": "gpt-4o-mini"}],
        price_in_per_1k_tokens=0.1, price_out_per_1k_tokens=0.4,
    )
    base.update(kw)
    return ProviderConfig(**base)


class OpenAICompatShim:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.key = cfg.key

    def matches(self, model: str) -> bool:
        return model.startswith(self.cfg.model_prefix) or model == self.key


def make_registry(*cfgs: ProviderConfig) -> Registry:
    reg = Registry.__new__(Registry)
    reg.config = AppConfig(providers={c.key: c for c in cfgs})
    reg.ctx = SimpleNamespace(config=AppConfig(), sessions=None, store=None)
    reg.adapters = {c.key: OpenAICompatShim(c) for c in cfgs}
    reg.throttles = {}
    return reg


class FakeGateway:
    """捕获每次调用的 system/prompt/model，返回可控文本。"""

    def __init__(self, registry, *, fail_models=()):
        self.registry = registry
        self.resolve = lambda m: ("openai", m)
        self.calls: list[dict] = []
        self._fail = set(fail_models)

    async def complete(self, *, model, system, prompt, mode):
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        if model in self._fail:
            raise RuntimeError(f"{model} 挂了")
        text = f"改写[{model}]\n\n第二段"
        return SimpleNamespace(text=text)


def _make_app(tmp_path, gateway, registry):
    store = StudioStore(tmp_path / "studio.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.state.registry = registry
    app.state.gateway = gateway
    app.include_router(studio_routes.router)
    return app


@pytest.fixture
def client(tmp_path):
    reg = make_registry(compat_cfg())
    gw = FakeGateway(reg)
    with TestClient(_make_app(tmp_path, gw, reg)) as c:
        c._gw = gw  # noqa: SLF001 - 测试内部挂引用，方便断言捕获的 prompt
        yield c


# ==================== 节内上下文 ====================


def test_context_is_injected_and_declared(client):
    client.post("/studio/transform", json={
        "text": "要加工的选区", "action": "rewrite",
        "context": "前文一句\n【……选区略……】\n后文一句",
    })
    call = client._gw.calls[0]
    assert "<context>" in call["prompt"] and "前文一句" in call["prompt"]
    # 关键那句护栏：不让模型把上下文一起吐出来
    assert "不要输出它" in call["system"]


def test_context_absent_when_empty(client):
    client.post("/studio/transform", json={"text": "只有正文", "action": "rewrite"})
    call = client._gw.calls[0]
    assert "<context>" not in call["prompt"]
    assert "不要输出它" not in call["system"]


def test_context_is_truncated(client):
    big = "字" * 5000
    client.post("/studio/transform", json={"text": "选区", "action": "rewrite", "context": big})
    prompt = client._gw.calls[0]["prompt"]
    # 只留窗口头部，不整块塞进 prompt
    assert big[: studio_routes.CONTEXT_MAX] in prompt
    assert big not in prompt


# ==================== 跨模型对比 ====================


def test_models_take_over_candidates(client):
    client.post("/studio/transform", json={
        "text": "选区", "action": "rewrite", "candidates": 3,
        "models": ["gpt-4o-mini", "gpt-4o"],
    })
    cand = client._gw.calls
    assert len(cand) == 2                       # 给了 models 就无视 candidates=3
    assert {c["model"] for c in cand} == {"gpt-4o-mini", "gpt-4o"}


def test_models_dedup_and_cap(client):
    client.post("/studio/transform", json={
        "text": "选区", "action": "rewrite",
        "models": ["gpt-a", "", "gpt-a", "gpt-b", "gpt-c", "gpt-d"],
    })
    models = [c["model"] for c in client._gw.calls]
    assert models == ["gpt-a", "gpt-b", "gpt-c"]  # 去重保序，至多 3


def test_candidates_still_works_without_models(client):
    client.post("/studio/transform", json={"text": "选区", "action": "rewrite", "candidates": 2})
    assert len(client._gw.calls) == 2
    # 没给 models 时全部落到同一个默认模型
    assert client._gw.calls[0]["model"] == client._gw.calls[1]["model"]


def test_each_candidate_carries_its_model(client):
    r = client.post("/studio/transform", json={
        "text": "选区", "action": "rewrite", "models": ["gpt-4o-mini", "gpt-4o"],
    }).json()
    got = {c["model"] for c in r["candidates"]}
    assert got == {"gpt-4o-mini", "gpt-4o"}
    assert all("model" in c and "variant" in c for c in r["candidates"])


def test_one_model_failing_keeps_others(tmp_path):
    reg = make_registry(compat_cfg())
    gw = FakeGateway(reg, fail_models={"gpt-bad"})
    with TestClient(_make_app(tmp_path, gw, reg)) as c:
        r = c.post("/studio/transform", json={
            "text": "选区", "action": "rewrite", "models": ["gpt-bad", "gpt-good"],
        })
    body = r.json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["model"] == "gpt-good"
    assert any("gpt-bad" in e for e in body["errors"])
