"""连载节奏与发表回流（缺口 6 后端）：存稿还能日更几天、手填台账的轻聚合。

口径都是定过的：速度优先近 14 个日历天的实际日均，没写过退回每日目标，
两个都没有就给 null——编一个假天数比不给更糟。存稿取 `final` 定稿总量
（没有逐节已发布位），归档作品不算存货。
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


async def make_final_node(store, *, title: str, content: str, archived: bool = False) -> dict:
    """造一节定稿：正文在 create_node 就填好（update_node 只改状态，不落 write_days）。"""
    w = await store.create_work(kind="novel", title=f"书·{title}")
    if archived:
        await store.update_work(w["id"], status="archived")
    n = await store.create_node(work_id=w["id"], kind="chapter", title=title, content=content)
    await store.update_node(n["id"], status="final")
    return n


async def test_stock_and_history_pace(studio) -> None:
    """有历史节奏：分母是窗口内日均，天数 = 存稿 ÷ 速度，向下取整。"""
    client, store = studio
    await make_final_node(store, title="甲", content="第一稿" * 100)
    await make_final_node(store, title="乙", content="第二稿" * 80)
    # 手写一笔今天的：update_node 带正文且来源 human → write_days 落账
    n = await store.create_node(
        work_id=(await store.list_works())[0]["id"], kind="chapter", title="丙")
    r = await client.patch(f"/studio/nodes/{n['id']}", json={"content": "今天手写的一段正文。"})
    assert r.status_code == 200

    j = (await client.get("/studio/stats/pipeline")).json()
    assert j["stock_nodes"] == 2
    assert j["stock_words"] > 0
    assert j["window_chars"] > 0
    assert j["pace_source"] == "history"
    pace = j["window_chars"] / j["days"]
    assert j["days_left"] == int(j["stock_words"] // pace)


async def test_pace_falls_back_to_target(studio) -> None:
    """窗口内一笔没写：退回每日目标当分母。"""
    client, store = studio
    await make_final_node(store, title="甲", content="存稿内容" * 300)
    r = await client.put("/studio/settings/daily_target_words", json={"value": "1000"})
    assert r.status_code == 200

    j = (await client.get("/studio/stats/pipeline")).json()
    assert j["window_chars"] == 0
    assert j["pace_source"] == "target"
    assert j["pace"] == 1000
    assert j["days_left"] == int(j["stock_words"] // 1000)


async def test_no_pace_no_fake_number(studio) -> None:
    """既没写过也没设目标：days_left 给 null，不编数。"""
    client, store = studio
    await make_final_node(store, title="甲", content="一段定稿正文。")
    j = (await client.get("/studio/stats/pipeline")).json()
    assert j["pace_source"] is None
    assert j["days_left"] is None
    assert j["stock_words"] > 0   # 存稿本身照报：信息不为零，只是不换算天数


async def test_archived_work_is_not_stock(studio) -> None:
    """归档的书不算存货：那本书都不更了，字数别虚胖预警。"""
    client, store = studio
    await make_final_node(store, title="还在写", content="活稿" * 100)
    await make_final_node(store, title="已放弃", content="死稿" * 100, archived=True)
    j = (await client.get("/studio/stats/pipeline")).json()
    assert j["stock_nodes"] == 1


async def test_insight_aggregates_manual_ledger(studio) -> None:
    """回流聚合：总量、按文体分组；不足十篇不给趋势（前五比再前五没意义）。"""
    client, store = studio
    a = await store.create_work(kind="article", title=" tech 文", subtype="tech")
    b = await store.create_work(kind="article", title="历史文", subtype="history")
    for reads, likes, wid in ((100, 5, a["id"]), (300, 15, a["id"]), (80, 2, b["id"])):
        r = await client.post(f"/studio/works/{wid}/publications", json={
            "published_at": "2026-11-01", "reads": reads, "likes": likes})
        assert r.status_code == 200

    j = (await client.get("/studio/publications/insight")).json()
    assert j["pubs"] == 3 and j["works"] == 2
    assert j["reads_total"] == 480 and j["reads_avg"] == 160.0
    tops = j["by_subtype"]
    assert {t["subtype"] for t in tops} == {"tech", "history"}
    tech = next(t for t in tops if t["subtype"] == "tech")
    assert tech["pubs"] == 2 and tech["reads_avg"] == 200.0
    # 按平均阅读降序：tech(200) 该排在 history(80) 前面
    assert tops[0]["subtype"] == "tech"
    assert j["trend"] is None


async def test_insight_trend_needs_ten_records(studio) -> None:
    """满十篇才给趋势，且是「最近五篇 vs 前五篇」。"""
    client, store = studio
    w = await store.create_work(kind="article", title="批量文")
    for i in range(10):
        # published_at 递增：排序后最后五条是 reads=200 的那批
        day = f"2026-10-{i + 1:02d}"
        reads = 100 if i < 5 else 200
        await client.post(f"/studio/works/{w['id']}/publications", json={
            "published_at": day, "reads": reads})
    j = (await client.get("/studio/publications/insight")).json()
    assert j["trend"] == {"recent_avg": 200.0, "prev_avg": 100.0}


async def test_empty_ledger_is_not_an_error(studio) -> None:
    """没记过台账：回全零形状而不是 500。"""
    client, _store = studio
    j = (await client.get("/studio/publications/insight")).json()
    assert j["pubs"] == 0 and j["by_subtype"] == [] and j["trend"] is None


async def test_target_setting_key_whitelisted(studio) -> None:
    """新键进白名单的同时，乱键照旧拒——闸门不是只开不关。"""
    client, _store = studio
    r = await client.put("/studio/settings/daily_target_words", json={"value": "800"})
    assert r.status_code == 200
    r = await client.put("/studio/settings/evil_key", json={"value": "x"})
    assert r.status_code == 400
