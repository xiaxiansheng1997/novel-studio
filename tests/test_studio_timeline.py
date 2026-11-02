"""图谱时间轴：`GET /studio/works/{id}/graph/timeline` 把事件与人物状态摆成一条线。

前端「时间轴」视图只负责摆位置，所以语义加工都在这一层：participants 换成名字、
state_json 解析成对象、按节给出可跳转的 sections。空图返回三个空数组——刚立完项
就点开时间轴不是错误。

坏 `state_json` 也不能让整条时间轴 500：抽取模型偶尔会往里面塞半截 JSON，
那是展示问题，不是数据丢失，退化成空对象继续显示其余部分。
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


def timeline_url(work_id: str) -> str:
    return f"/studio/works/{work_id}/graph/timeline"


async def make_novel(client, title: str = "时间轴测试") -> str:
    r = await client.post("/studio/works",
                          json={"kind": "novel", "title": title, "scale": "series"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ==================== 空图与非小说 ====================

async def test_empty_graph_returns_three_empty_arrays(studio) -> None:
    """还没抽过：三个空数组，不 404 也不 500；单篇文档问时间轴是 400。"""
    client, _ = studio
    work_id = await make_novel(client)

    r = await client.get(timeline_url(work_id))

    assert r.status_code == 200
    assert r.json() == {"sections": [], "events": [], "states": []}

    article = (await client.post("/studio/works",
                                 json={"kind": "article", "title": "散文"})).json()
    assert (await client.get(timeline_url(article["id"]))).status_code == 400


# ==================== 事件与人物状态 ====================

async def test_events_are_sorted_by_ordinal(studio) -> None:
    """先写第 2 章的事、再写第 1 章的：返回顺序必须按章号，sections 也一样。"""
    client, store = studio
    work_id = await make_novel(client)
    c2 = await store.create_node(work_id=work_id, kind="chapter", title="第二章", ordinal=2)
    c1 = await store.create_node(work_id=work_id, kind="chapter", title="第一章", ordinal=1)
    await store.add_event(work_id, ordinal=2, summary="第二章的事", participants=[],
                          story_time="第二天", importance=3)
    await store.add_event(work_id, ordinal=1, summary="第一章的事", participants=[],
                          story_time="夜里", importance=1)

    body = (await client.get(timeline_url(work_id))).json()

    assert [s["ordinal"] for s in body["sections"]] == [1, 2]
    assert [s["title"] for s in body["sections"]] == ["第一章", "第二章"]
    assert {s["id"] for s in body["sections"]} == {c1["id"], c2["id"]}
    assert [(e["ordinal"], e["summary"]) for e in body["events"]] == [
        (1, "第一章的事"), (2, "第二章的事")]
    assert [e["story_time"] for e in body["events"]] == ["夜里", "第二天"]
    assert [e["importance"] for e in body["events"]] == [1, 3]
    assert body["states"] == []


async def test_participants_are_mapped_to_names(studio) -> None:
    """库里存的是实体 id，时间轴上要给人看的名字；状态行也带名字。"""
    client, store = studio
    work_id = await make_novel(client)
    await store.create_node(work_id=work_id, kind="chapter", title="第一章", ordinal=1)
    a = await store.upsert_entity(work_id, kind="character", name="阿泠", canonical="阿泠",
                                  first_ordinal=1)
    b = await store.upsert_entity(work_id, kind="character", name="老周", canonical="老周",
                                  first_ordinal=1)
    await store.add_event(work_id, ordinal=1, summary="两人对坐", participants=[a["id"], b["id"]])
    await store.add_state(work_id, a["id"], 1, '{"status": "受伤", "where": "渡口"}')

    body = (await client.get(timeline_url(work_id))).json()

    assert body["events"][0]["participants"] == ["阿泠", "老周"]
    assert body["states"] == [{
        "entity_id": a["id"], "name": "阿泠", "ordinal": 1,
        "state": {"status": "受伤", "where": "渡口"},
    }]


async def test_broken_state_json_degrades_to_empty_object(studio) -> None:
    """半截 JSON / 非对象都退化成 `{}`，同一节里的其他行照常返回。"""
    client, store = studio
    work_id = await make_novel(client)
    await store.create_node(work_id=work_id, kind="chapter", title="第一章", ordinal=1)
    a = await store.upsert_entity(work_id, kind="character", name="阿泠", canonical="阿泠")
    await store.add_state(work_id, a["id"], 1, '{"status": "受')
    await store.add_state(work_id, a["id"], 2, '["不是对象"]')

    body = (await client.get(timeline_url(work_id))).json()

    assert [s["state"] for s in body["states"]] == [{}, {}]
    assert [s["name"] for s in body["states"]] == ["阿泠", "阿泠"]
    assert body["sections"][0]["ordinal"] == 1
