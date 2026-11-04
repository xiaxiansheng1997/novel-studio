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

    def matches_exact(self, model: str) -> bool:
        # 与基类同语义：route 三段式先问显式声明，鸭子替身不能少这个口子
        return model == self.key or any(m.id == model for m in self.cfg.models)

    def matches(self, model: str) -> bool:
        return self.matches_exact(model) or model.startswith(self.cfg.model_prefix)


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


def _seed_work(client, chapters: int, with_content: str = "") -> str | tuple:
    work = client.post("/studio/works", json={"kind": "novel", "title": "预算作品",
                                              "scale": "series"}).json()
    vol = client.post(f"/studio/works/{work['id']}/nodes",
                      json={"kind": "volume", "title": "第一卷"}).json()
    ids = []
    for i in range(chapters):
        node = client.post(f"/studio/works/{work['id']}/nodes",
                           json={"kind": "chapter", "title": f"第{i}章", "parent_id": vol["id"],
                                 "content": with_content}).json()
        ids.append(node["id"])
    if with_content:
        return work["id"], ids
    return work["id"]


def _seed_ok_run(client, work_id, *, task_type, prompt, out):
    """往 runs 表摆一条成功历史：走 TestClient 的 portal（call 只收函数与位置
    参数，关键字得包在闭包里），不碰 store 私有接口。"""
    store = client.app.state.studio_store
    rid = client.portal.call(lambda: store.start_run(
        work_id=work_id, node_id=None, task_type=task_type,
        model="gpt-4o-mini", provider="openai", prompt_chars=prompt))
    client.portal.call(lambda: store.finish_run(rid, status="ok", output_chars=out))


async def _queue_start():
    """worker 替身：闸门测试只走到入队，不碰真模型。"""


async def _queue_status(work_id: str = ""):
    return {"queued": 0, "active": False}


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


# ==================== 审查回归：M1 / m10 / m14 ====================


async def test_run_baselines_includes_queue_draft(store):
    """审查 M1：pipeline 落 article_draft、队列 worker 落 draft——缺省得算这一档
    的两个名字，不然小说批量跑得再多历史中位数也永远取不到。"""
    wid = (await store.create_work(kind="novel", title="队列账", scale="series"))["id"]
    for _ in range(3):
        rid = await store.start_run(work_id=wid, node_id=None, task_type="draft",
                                    model="m", provider="p", prompt_chars=9000)
        await store.finish_run(rid, status="ok", output_chars=1500)

    base = await store.run_baselines(wid)
    assert base["count"] == 3
    assert base["output_median"] == 1500
    # 显式指定时仍只查那一个：不混账
    assert (await store.run_baselines(wid, task_type="article_draft"))["count"] == 0


async def test_output_chars_total_ok_only(store):
    wid = (await store.create_work(kind="novel", title="合计", scale="series"))["id"]
    r1 = await store.start_run(work_id=wid, node_id=None, task_type="draft",
                               model="m", provider="p", prompt_chars=1)
    await store.finish_run(r1, status="ok", output_chars=2000)
    r2 = await store.start_run(work_id=wid, node_id=None, task_type="summarize",
                               model="m", provider="p", prompt_chars=1)
    await store.finish_run(r2, status="ok", output_chars=500)
    r3 = await store.start_run(work_id=wid, node_id=None, task_type="draft",
                               model="m", provider="p", prompt_chars=1)
    await store.finish_run(r3, status="failed", output_chars=9999)  # 没结算的不算
    assert await store.output_chars_total(wid) == 2500
    assert await store.output_chars_total("w_不存在") == 0


async def test_budget_is_total_not_per_run(store):
    """审查 m10：上限是总额度——同一本书每次 1.8、上限 5.0，
    第三次带着「已花 3.6」必须被拦；只比本次的话能无限点。"""
    wid = (await store.create_work(kind="novel", title="两连", scale="series"))["id"]
    reg = make_registry(compat_cfg())
    await cost.set_budget_limit(store, 5.0)

    first = await cost.estimate_pipeline(store, reg, work_id=wid, sections=2, model="gpt-4o-mini")
    # 没历史走默认：2 节共 24000 输入字 + 3000 输出字，÷2 得 12000+1500 token，
    # 12×0.1 + 1.5×0.4 = 1.8
    assert first["priced"] is True
    assert first["est_cost"] == pytest.approx(1.8)
    assert first["spent_before"] == 0.0
    assert first["over_budget"] is False   # 1.8 ≤ 5：第一次放行

    second = await cost.estimate_pipeline(
        store, reg, work_id=wid, sections=2, model="gpt-4o-mini",
        spent_before=first["est_cost"],
    )
    assert second["projected_total"] == pytest.approx(3.6)
    assert second["over_budget"] is False  # 3.6 ≤ 5：第二次也还在额度里

    # 连点到第三次：已花 3.6 + 本次 1.8 = 5.4 越线；只比本次的话永远追不上上限
    third = await cost.estimate_pipeline(
        store, reg, work_id=wid, sections=2, model="gpt-4o-mini",
        spent_before=second["projected_total"],
    )
    assert third["over_budget"] is True


def test_estimate_route_reports_spent(tmp_path):
    """路由层把账本接上：有历史成功 run 时，estimate 回里得带 spent_before，
    且「已花 + 本次」才是和上限比的那个数。"""
    reg = make_registry(compat_cfg())
    with _build_client(tmp_path, reg) as client:
        wid = _seed_work(client, 1)
        _seed_ok_run(client, wid, task_type="draft", prompt=1000, out=3000)
        client.put("/studio/budget", json={"limit": 1.4})

        est = client.post(f"/studio/works/{wid}/pipeline/estimate", json={}).json()
        # 已花 = 3000 字 ÷ 2 字/token ÷ 1000 × 0.4 = 0.6；本次 1 节默认口径 0.9，合 1.5 > 1.4
        # ——只比本次的话 0.9 ≤ 1.4 会放行，账本在这里真正改变了结论
        assert est["est_cost"] == pytest.approx(0.9)
        assert est["spent_before"] == pytest.approx(0.6)
        assert est["projected_total"] == pytest.approx(1.5)
        assert est["over_budget"] is True
        assert client.post(f"/studio/works/{wid}/pipeline", json={}).status_code == 402


def test_batch_gate_covers_all_model_tasks(tmp_path):
    """审查 m14：beats/polish/extract/summarize 走的也是计钱通道，闸门不再只拦 draft；
    check 是纯本地规则不花钱，不拦。"""
    reg = make_registry(compat_cfg())
    with _build_client(tmp_path, reg) as client:
        client.app.state.studio_queue = SimpleNamespace(
            start=_queue_start, status=_queue_status)
        wid, _chapters = _seed_work(client, 2, with_content="正文内容若干")
        client.put("/studio/budget", json={"limit": 0.01})

        # 种子章节都带正文：闸门之前的选节靠 only_empty 筛，draft 与那几个
        # 「只挑有正文节」的任务都要能走到闸门才验得到拦不拦
        for task in ("draft", "summarize", "polish", "extract", "beats"):
            r = client.post(f"/studio/works/{wid}/batch",
                            json={"task_type": task, "only_empty": False})
            assert r.status_code == 402, task

        # check 不在名单里：不估不拦，能走到入队
        r = client.post(f"/studio/works/{wid}/batch",
                        json={"task_type": "check", "only_empty": False})
        assert r.status_code == 200
        assert r.json()["queued"] == 2
