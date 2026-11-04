"""质量反馈环：采纳率靠 node_versions 的 run_id 线，存活率靠字符二元组 Dice。

口径跟数字一样硬：transform/outline 的采用不写版本行，剔出分母而不是
假报 0%；采用后只挪空白不算重写；真改了正文必须记成没活下来。
埋点走的是前端同款路径（PATCH 带 run_id），顺带验证路由 pop 干净了——
nodes 表没有 run_id 列，漏 pop 就是一条撞列的 UPDATE。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import studio_routes
from app.studio.store import StudioStore, _dice_bigrams


@pytest.fixture
async def studio(tmp_path):
    """同一事件循环里起 store 与 ASGI 客户端：aiosqlite 的连接不能跨循环用。"""
    store = StudioStore(tmp_path / "studio.db")
    await store.init()
    app = FastAPI()
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.include_router(studio_routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store
    await store.close()


async def ok_run(store, work_id, node_id, task_type, model) -> str:
    rid = await store.start_run(work_id=work_id, node_id=node_id, task_type=task_type,
                                model=model, provider="p", prompt_chars=100)
    await store.finish_run(rid, status="ok", output_chars=500, elapsed_ms=1000)
    return rid


async def adopt(client, node_id, content, *, source, run_id) -> None:
    r = await client.patch(f"/studio/nodes/{node_id}", json={
        "content": content, "snapshot": True,
        "snapshot_source": source, "run_id": run_id,
    })
    assert r.status_code == 200


async def seed(store, client) -> dict:
    a = await store.create_work(kind="novel", title="反馈甲")
    n1 = await store.create_node(work_id=a["id"], kind="chapter", title="第一节")
    n2 = await store.create_node(work_id=a["id"], kind="chapter", title="第二节")

    # r1：采用后只挪过空白 → 算采纳、算原样活着
    r1 = await ok_run(store, a["id"], n1["id"], "article_draft", "alpha")
    await adopt(client, n1["id"], "山，一座山。", source="ai_draft", run_id=r1)
    await client.patch(f"/studio/nodes/{n1['id']}", json={"content": "山， 一座山 。"})
    # r2：同模型再跑一次，没人采用 → 压低 alpha 的采纳率
    r2 = await ok_run(store, a["id"], n1["id"], "article_draft", "alpha")

    # r3：采用后人真的重写了 → 算采纳、算没活下来
    r3 = await ok_run(store, a["id"], n2["id"], "polish", "beta")
    await adopt(client, n2["id"], "从前有座山，山里有个老和尚在讲故事。",
                source="ai_polish", run_id=r3)
    await client.patch(f"/studio/nodes/{n2['id']}", json={
        "content": "完全不同的另一段文字，主语谓语全换了，一个字都不剩。",
        "snapshot": True,
    })

    # 采用不写版本的两类：只许被剔出分母
    await ok_run(store, a["id"], n1["id"], "transform:rewrite", "alpha")
    await ok_run(store, a["id"], None, "outline", "beta")
    # 失败的 run：压根不进采纳率
    rid = await store.start_run(work_id=a["id"], node_id=n1["id"], task_type="article_draft",
                                model="alpha", provider="p", prompt_chars=100)
    await store.finish_run(rid, status="failed", error="超时", elapsed_ms=100)
    return {"a": a, "n1": n1, "n2": n2, "r1": r1, "r2": r2, "r3": r3}


# ==================== 相似度原语 ====================

def test_dice_whitespace_insensitive_and_extremes() -> None:
    assert _dice_bigrams("山， 一座山 。", "山，一座山。") == 1.0   # 只挪空白 = 没改
    assert _dice_bigrams("abcd", "wxyz") == 0.0
    assert _dice_bigrams("", "") == 1.0
    assert _dice_bigrams("山", "山") == 1.0
    assert _dice_bigrams("山", "水") == 0.0                          # 凑不出二元组
    # 改一小段：分数应落在中间且明显低于阈值 0.995
    got = _dice_bigrams("从前有座山，山里有个老和尚在讲故事。", "从前有座山，山顶有个庙在敲钟。")
    assert 0.0 < got < 0.995


# ==================== 采纳率 ====================

async def test_adopt_rate_and_exclusions(studio) -> None:
    client, store = studio
    ids = await seed(store, client)

    got = await store.feedback()
    assert got["adopt"]["total"] == 3          # r1 r2 r3；transform/outline/failed 都不在
    assert got["adopt"]["adopted"] == 2
    assert got["adopt"]["rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert got["excluded_runs"] == 2
    assert got["window_capped"] is False

    by_task = {t["task_type"]: t for t in got["by_task"]}
    assert set(by_task) == {"article_draft", "polish"}   # transform:* 不该冒出来
    assert by_task["article_draft"] == {
        "task_type": "article_draft", "total": 2, "adopted": 1, "rate": 0.5}

    by_model = {m["model"]: m for m in got["by_model"]}
    assert by_model["alpha"]["total"] == 2 and by_model["alpha"]["adopted"] == 1
    assert by_model["beta"]["rate"] == 1.0

    by_work = {w["work_id"]: w for w in got["by_work"]}
    w = by_work[ids["a"]["id"]]
    assert w["title"] == "反馈甲" and w["total"] == 3 and w["adopted"] == 2


async def test_adopt_written_by_http_path_reaches_versions(studio) -> None:
    """run_id 真的落在版本行上才算埋点接通；nodes 表没被撞也在这里一并验证。"""
    client, store = studio
    ids = await seed(store, client)

    versions = await store.list_versions(ids["n1"]["id"])
    assert versions[0]["run_id"] == ids["r1"]   # 最新一版就是 r1 那次采用

    node = await store.get_node(ids["n1"]["id"])
    assert node["content"] == "山， 一座山 。"   # 正文照常更新，没被 run_id 带偏


# ==================== 存活率 ====================

async def test_survive_counts_untouched_and_rewritten(studio) -> None:
    client, store = studio
    await seed(store, client)

    got = await store.feedback()
    s = got["survive"]
    assert s["checked"] == 2                    # 两条带 run_id 的 AI 版本
    assert s["untouched"] == 1                  # 只有挪空白那条算原样采用
    assert s["rate"] == 0.5
    assert s["threshold"] == 0.995


async def test_survive_without_later_human_uses_current(studio) -> None:
    """采用后没有任何版本、但自动保存改了正文：拿当前正文比，改动照样漏不掉。"""
    client, store = studio
    a = await store.create_work(kind="novel", title="无手改")
    n = await store.create_node(work_id=a["id"], kind="chapter", title="一")
    rid = await ok_run(store, a["id"], n["id"], "article_draft", "alpha")
    await adopt(client, n["id"], "风和日丽，适合写一段长一点的正文。",
                source="ai_draft", run_id=rid)
    await client.patch(f"/studio/nodes/{n['id']}", json={
        "content": "风和日丽，适合写一段长一点的正文。后来有人加了这一句。"})

    got = await store.feedback()
    assert got["survive"]["checked"] == 1 and got["survive"]["untouched"] == 0


# ==================== 空库与端点 ====================

async def test_empty_library_is_all_zero(studio) -> None:
    client, _ = studio
    got = await client.get("/studio/feedback")
    assert got.status_code == 200

    j = got.json()
    assert j["adopt"] == {"total": 0, "adopted": 0, "rate": 0.0}
    assert j["by_model"] == [] and j["by_task"] == [] and j["by_work"] == []
    assert j["survive"]["checked"] == 0 and j["survive"]["rate"] == 0.0
    assert j["excluded_runs"] == 0 and j["window_capped"] is False


async def test_endpoint_sample_clamped(studio) -> None:
    """sample 被 store 夹到 [1, 500]：传 0 或天价都不该把查询拼歪。"""
    client, store = studio
    await seed(store, client)

    r = await client.get("/studio/feedback?sample=0")
    assert r.status_code == 200
    assert r.json()["survive"]["checked"] >= 1   # 夹到下限 1，仍有得比

    r = await client.get("/studio/feedback?sample=999999")
    assert r.status_code == 200
