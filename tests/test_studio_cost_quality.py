"""生成账本：runs 的耗时/字数按作品与按节汇总，再叠未处理硬告警。

成本只看真的跑过的书与节；质量只看两样落过库的东西——跑批打分与
未处理 error 告警。没打分的跑批不按 0 分算，而是不进均分。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import studio_routes
from app.studio.store import StudioStore


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


async def record(store, *, work_id, node_id, task_type, status, elapsed_ms=0,
                 prompt=0, output=0, score=None) -> str:
    rid = await store.start_run(
        work_id=work_id, node_id=node_id, task_type=task_type,
        model="m", provider="p", prompt_chars=prompt,
    )
    if status != "running":
        await store.finish_run(
            rid, status=status, output_chars=output, elapsed_ms=elapsed_ms, score=score,
        )
    return rid


async def seed(store) -> dict:
    a = await store.create_work(kind="novel", title="成本甲")
    b = await store.create_work(kind="novel", title="成本乙")
    c1 = await store.create_node(work_id=a["id"], kind="chapter", title="第一章")
    c2 = await store.create_node(work_id=a["id"], kind="chapter", title="第二章")
    b1 = await store.create_node(work_id=b["id"], kind="chapter", title="乙一")

    await record(store, work_id=a["id"], node_id=c1["id"], task_type="chapter_draft",
                 status="ok", elapsed_ms=4000, prompt=1000, output=2000, score=8)
    await record(store, work_id=a["id"], node_id=c1["id"], task_type="chapter_draft",
                 status="ok", elapsed_ms=3000, prompt=900, output=1800, score=6)
    await record(store, work_id=a["id"], node_id=c2["id"], task_type="chapter_revise",
                 status="failed", elapsed_ms=1000, prompt=500)
    await record(store, work_id=a["id"], node_id=c2["id"], task_type="chapter_draft",
                 status="running", prompt=300)
    # 全书级任务（大纲）：进整体与按任务，不进「按节」
    await record(store, work_id=a["id"], node_id=None, task_type="outline",
                 status="ok", elapsed_ms=2000, prompt=200, output=600, score=9)
    await record(store, work_id=b["id"], node_id=b1["id"], task_type="chapter_draft",
                 status="ok", elapsed_ms=500, prompt=100, output=100, score=10)

    await store.add_issue(a["id"], rule="dead_character", message="死者说话",
                          node_id=c1["id"], severity="error")
    await store.add_issue(a["id"], rule="item_conflict", message="道具已毁",
                          node_id=c1["id"], severity="error")
    await store.add_issue(a["id"], rule="fact_stale", message="事实过期",
                          node_id=c2["id"], severity="warn")
    gone = await store.add_issue(a["id"], rule="time_regression", message="时间倒流",
                                 node_id=c2["id"], severity="error")
    await store.resolve_issue(gone["id"])
    # 全书级硬告警：算作品质量，但不落到某一节
    await store.add_issue(b["id"], rule="citation_missing", message="缺出处", severity="error")
    return {"a": a, "b": b, "c1": c1, "c2": c2, "b1": b1}


# ==================== 汇总口径 ====================

async def test_overall_and_task_breakdown(studio) -> None:
    _, store = studio
    await seed(store)

    got = await store.cost_quality()
    o = got["overall"]

    assert o["runs"] == 6 and o["ok"] == 4 and o["failed"] == 1
    assert o["interrupted"] == 0 and o["running"] == 1 and o["done"] == 5
    assert o["elapsed_ms"] == 10500 and o["avg_elapsed_ms"] == 2100
    assert o["prompt_chars"] == 3000 and o["output_chars"] == 4500
    # 均分只算打过分的 4 次：8/6/9/10
    assert o["scored"] == 4 and o["avg_score"] == 8.25
    assert o["last_at"]

    by_task = {t["task_type"]: t for t in got["by_task"]}
    assert set(by_task) == {"chapter_draft", "chapter_revise", "outline"}
    # 两本书都用 chapter_draft：4 次里 3 次打分，running 那次不进均分
    assert by_task["chapter_draft"]["runs"] == 4
    assert by_task["chapter_draft"]["elapsed_ms"] == 7500
    assert by_task["chapter_draft"]["scored"] == 3
    assert by_task["chapter_draft"]["avg_score"] == 8.0
    assert by_task["outline"]["runs"] == 1
    assert got["by_task"][0]["task_type"] == "chapter_draft"  # 按耗时降序


async def test_works_rows_carry_errors_and_quality(studio) -> None:
    _, store = studio
    await seed(store)

    got = await store.cost_quality()
    works = {w["work_title"]: w for w in got["works"]}

    a = works["成本甲"]
    assert a["runs"] == 5 and a["done"] == 4 and a["running"] == 1
    assert a["elapsed_ms"] == 10000 and a["avg_elapsed_ms"] == 2500
    assert a["avg_score"] == 7.67
    assert a["open_errors"] == 2

    b = works["成本乙"]
    assert b["runs"] == 1 and b["avg_score"] == 10.0
    # 全书级硬告警算到作品，不算到节
    assert b["open_errors"] == 1
    assert got["works"][0]["work_title"] == "成本甲"


async def test_node_rows_join_titles_and_skip_work_level_runs(studio) -> None:
    _, store = studio
    await seed(store)

    got = await store.cost_quality()
    nodes = {n["node_title"]: n for n in got["nodes"]}

    assert set(nodes) == {"第一章", "第二章", "乙一"}
    first = nodes["第一章"]
    assert first["work_title"] == "成本甲"
    assert first["runs"] == 2 and first["elapsed_ms"] == 7000
    assert first["avg_score"] == 7.0
    assert first["open_errors"] == 2
    # warn 与已处理的 error 都不算
    assert nodes["第二章"]["open_errors"] == 0
    assert nodes["第二章"]["avg_score"] is None
    assert nodes["乙一"]["open_errors"] == 0
    assert all(n["node_id"] for n in got["nodes"])


async def test_limits_only_shape_rows(studio) -> None:
    _, store = studio
    await seed(store)

    got = await store.cost_quality(work_limit=1, node_limit=1)

    assert got["limits"] == {"works": 1, "nodes": 1}
    assert [w["work_title"] for w in got["works"]] == ["成本甲"]
    assert [n["node_title"] for n in got["nodes"]] == ["第一章"]
    # 行数受限，统计口径不变
    assert got["overall"]["runs"] == 6


async def test_empty_database(studio) -> None:
    client, _ = studio
    r = await client.get("/studio/stats/cost-quality")
    assert r.status_code == 200

    j = r.json()
    assert j["overall"]["runs"] == 0
    assert j["overall"]["avg_score"] is None
    assert j["overall"]["avg_elapsed_ms"] == 0
    assert j["works"] == [] and j["nodes"] == [] and j["by_task"] == []


async def test_endpoint_passes_limits(studio) -> None:
    client, store = studio
    await seed(store)

    r = await client.get("/studio/stats/cost-quality?works=30&nodes=30")
    assert r.status_code == 200

    j = r.json()
    assert j["limits"] == {"works": 30, "nodes": 30}
    assert j["works"][0]["work_title"] == "成本甲"
    assert j["overall"]["runs"] == 6
