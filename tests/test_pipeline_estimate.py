"""迭代 C：批量生成前的成本预估与预算上限。

口径三条：
1. **预估吃本书自己的账**——历史成功 runs 的字数中位数（≥3 条才敢用），
   样本不足退回保守默认并在 basis 里明说；
2. **只给计钱的通道算钱**——compat 有单价才算 est_cost；反代 priced=False，
   预算拦不到它（它的成本是账号风控，不是账单）；
3. **超预算拦 402，ignore_budget 放行**——拦手滑，不拦清醒的决定。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.config import AppConfig, ProviderConfig
from app.core.errors import ProviderError
from app.core.registry import Registry
from app.studio import cost
from app.studio.store import StudioStore


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


def compat_cfg(**kw) -> ProviderConfig:
    base = dict(
        key="openai", compat=True, base_url="https://api.example/v1",
        model_prefix="gpt-", models=[{"id": "gpt-4o-mini"}],
        price_in_per_1k_tokens=0.1, price_out_per_1k_tokens=0.4,
    )
    base.update(kw)
    return ProviderConfig(**base)


def make_registry(cfg: ProviderConfig) -> Registry:
    ctx = SimpleNamespace(config=AppConfig(), sessions=None, store=None)
    reg = Registry.__new__(Registry)
    reg.config = AppConfig(providers={cfg.key: cfg})
    reg.ctx = ctx
    reg.adapters = {cfg.key: OpenAICompatShim(cfg)}
    reg.throttles = {}
    return reg


class OpenAICompatShim:
    """够用的适配器替身：route 的行为与真品一致即可（不碰 httpx）。"""

    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.key = cfg.key

    def matches(self, model: str) -> bool:
        return model.startswith(self.cfg.model_prefix) or model == self.key


# ==================== 纯函数与 store 基线 ====================


def test_median_odd_even_empty():
    assert cost.median([]) == 0
    assert cost.median([5]) == 5
    assert cost.median([9, 1, 5]) == 5          # 奇数：正中
    assert cost.median([2, 4, 6, 8]) == 5       # 偶数：中间两数平均
    assert cost.median([100, 1, 2]) == 2        # 一个极端值拉不动中位数


async def test_run_baselines_only_counts_ok_runs(store):
    wid = (await store.create_work(kind="novel", title="账本", scale="series"))["id"]
    for prompt, out, status in [
        (9000, 1200, "ok"), (11000, 1600, "ok"), (10000, 1400, "ok"),
        (60000, 50, "failed"),  # 失败的不算：会同时高估输入、低估输出
    ]:
        rid = await store.start_run(work_id=wid, node_id=None, task_type="article_draft",
                                    model="m", provider="p", prompt_chars=prompt)
        await store.finish_run(rid, status=status, output_chars=out)

    base = await store.run_baselines(wid)
    assert base["count"] == 3
    assert base["prompt_median"] == 10000
    assert base["output_median"] == 1400
    # 别的 task_type 不串账
    assert (await store.run_baselines(wid, task_type="polish"))["count"] == 0


async def test_budget_roundtrip_and_validation(store):
    assert await cost.budget_limit(store) == 0.0
    await cost.set_budget_limit(store, 12.5)
    assert await cost.budget_limit(store) == 12.5
    await cost.set_budget_limit(store, 0)          # 0 = 撤掉上限
    assert await cost.budget_limit(store) == 0.0
    with pytest.raises(ValueError):
        await cost.set_budget_limit(store, -1)
    # settings 里被写进脏东西时按「不设限」处理，不让批量整体瘫掉
    await store.set_setting("budget_limit", "不是数")
    assert await cost.budget_limit(store) == 0.0


# ==================== estimate_pipeline ====================


async def test_estimate_uses_history_median_and_prices(store):
    wid = (await store.create_work(kind="novel", title="预估", scale="series"))["id"]
    for p, o in [(8000, 1000), (12000, 2000), (10000, 1500)]:
        rid = await store.start_run(work_id=wid, node_id=None, task_type="article_draft",
                                    model="gpt-4o-mini", provider="openai", prompt_chars=p)
        await store.finish_run(rid, status="ok", output_chars=o)

    reg = make_registry(compat_cfg())
    est = await cost.estimate_pipeline(store, reg, work_id=wid, sections=4, model="gpt-4o-mini")

    assert est["basis"] == "runs_median"
    assert est["per_section_prompt_chars"] == 10000 and est["per_section_output_chars"] == 1500
    assert est["prompt_chars_total"] == 40000 and est["output_chars_total"] == 6000
    assert est["est_prompt_tokens"] == 20000 and est["est_output_tokens"] == 3000
    # 20k×0.1/1k + 3k×0.4/1k = 2.0 + 1.2
    assert est["priced"] is True and est["est_cost"] == pytest.approx(3.2)
    assert est["over_budget"] is False and est["budget_limit"] == 0.0

    await cost.set_budget_limit(store, 3.0)
    est2 = await cost.estimate_pipeline(store, reg, work_id=wid, sections=4, model="gpt-4o-mini")
    assert est2["over_budget"] is True


async def test_estimate_default_basis_and_unpriced(store):
    wid = (await store.create_work(kind="novel", title="新书", scale="series"))["id"]
    reg = make_registry(compat_cfg())
    est = await cost.estimate_pipeline(store, reg, work_id=wid, sections=2, model="gpt-4o-mini")
    assert est["basis"] == "default"          # 没历史：保守默认，不装精确
    assert est["per_section_prompt_chars"] == cost.DEFAULT_PROMPT_CHARS

    # 反代通道：照报字数，钱是 null；预算配了也拦不到它
    proxy_cfg = ProviderConfig(key="deepseek", model_prefix="deepseek-", models=[{"id": "deepseek-chat"}])
    reg2 = make_registry(proxy_cfg)
    await cost.set_budget_limit(store, 0.01)
    est2 = await cost.estimate_pipeline(store, reg2, work_id=wid, sections=2, model="deepseek-chat")
    assert est2["priced"] is False and est2["est_cost"] is None
    assert est2["over_budget"] is False
    assert est2["output_chars_total"] > 0     # 字数照报：反代的成本是账号，不是钱


# ==================== 路由层 ====================


def _build_client(tmp_path, registry):
    store = StudioStore(tmp_path / "studio.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    async def _boom_stream(**kwargs):
        # 放行路径不验证生成质量：一到真生成就报错，SSE 会走 node_error→done 收尾
        raise ProviderError("openai", "测试：不该到达真生成")
        yield  # pragma: no cover - 保持异步生成器签名

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.state.registry = registry
    app.state.gateway = SimpleNamespace(
        registry=registry, resolve=lambda m: ("openai", m), stream=_boom_stream,
    )
    app.include_router(studio_routes.router)
    return TestClient(app)


def _seed_work(client, chapters: int) -> str:
    work = client.post("/studio/works", json={"kind": "novel", "title": "预算作品",
                                              "scale": "series"}).json()
    vol = client.post(f"/studio/works/{work['id']}/nodes",
                      json={"kind": "volume", "title": "第一卷"}).json()
    for i in range(chapters):
        client.post(f"/studio/works/{work['id']}/nodes",
                    json={"kind": "chapter", "title": f"第{i}章", "parent_id": vol["id"],
                          "content": ""})
    return work["id"]


def test_estimate_and_budget_routes(tmp_path):
    reg = make_registry(compat_cfg())
    with _build_client(tmp_path, reg) as client:
        wid = _seed_work(client, 3)

        r = client.post(f"/studio/works/{wid}/pipeline/estimate", json={})
        assert r.status_code == 200
        est = r.json()
        assert est["sections"] == 3 and est["priced"] is True
        assert est["basis"] == "default" and est["over_budget"] is False

        # limit 收窄范围：预估跟着选节数走（与 run_pipeline 同一口径）
        r = client.post(f"/studio/works/{wid}/pipeline/estimate", json={"limit": 2})
        assert r.json()["sections"] == 2

        # 预算接口一对读写
        assert client.get("/studio/budget").json() == {"limit": 0.0}
        assert client.put("/studio/budget", json={"limit": 0.5}).status_code == 200
        assert client.get("/studio/budget").json() == {"limit": 0.5}
        assert client.put("/studio/budget", json={"limit": -1}).status_code == 422

        # 超预算：批量被 402 拦在门口，一个字都不该生成
        r = client.post(f"/studio/works/{wid}/pipeline", json={})
        assert r.status_code == 402
        assert "预算" in r.json()["detail"]

        # ignore_budget 放行：不再吃 402，SSE 能开起来（生成由替身报败，不影响边界）
        client.put("/studio/budget", json={"limit": 0.01})
        with client.stream("POST", f"/studio/works/{wid}/pipeline",
                           json={"ignore_budget": True, "limit": 1}) as resp:
            assert resp.status_code == 200


def test_estimate_route_404_unknown_work(tmp_path):
    reg = make_registry(compat_cfg())
    with _build_client(tmp_path, reg) as client:
        assert client.post("/studio/works/w_不存在/pipeline/estimate", json={}).status_code == 404


def test_batch_route_same_gate(tmp_path):
    """落库队列的 draft 任务与 pipeline 同一道预算闸门：关页面接着跑的路也要拦。"""
    reg = make_registry(compat_cfg())
    with _build_client(tmp_path, reg) as client:
        # 闸门在入队之前；queue 存不存在只决定能不能走到选节这一步
        client.app.state.studio_queue = SimpleNamespace()
        wid = _seed_work(client, 3)
        client.put("/studio/budget", json={"limit": 0.01})

        r = client.post(f"/studio/works/{wid}/batch", json={"task_type": "draft"})
        assert r.status_code == 402
        assert "预算" in r.json()["detail"]
