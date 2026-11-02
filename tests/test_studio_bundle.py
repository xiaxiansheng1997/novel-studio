"""作品包导出 / 导入：整本搬迁时 id 全部重发，引用跟着改。

包是自足的：树、历史版本、素材、事实、术语、设定卡与图谱都在里面；
chunks / summaries / runs 不在——那些从正文派生，导回去由路由重建一次。
导入只做「新增」：同一份包导两次得到两本独立的书，互不影响。
"""
from __future__ import annotations

import json
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


async def seed(store) -> dict:
    """一本带卷、带版本、带素材与图谱的小书。"""
    work = await store.create_work(
        kind="novel", title="迁移测试", scale="series", premise="渡口相遇",
        target_words=80000,
    )
    volume = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    chapter = await store.create_node(
        work_id=work["id"], kind="chapter", title="第一章", parent_id=volume["id"],
        content="阿泠走进渡口。",
    )
    await store.update_node(chapter["id"], content="阿泠走进渡口，老周在等。",
                            snapshot=True, snapshot_source="human")
    source = await store.add_source(work["id"], content="渡口在城东，冬天结冰。", title="笔记")
    await store.add_fact(work["id"], statement="渡口在城东", source_id=source["id"])
    await store.upsert_term(work["id"], term="阿泠", definition="主角")
    card = await store.upsert_card(work["id"], kind="character", name="阿泠", summary="刀客")
    a = await store.upsert_entity(work["id"], kind="character", name="阿泠",
                                  canonical="阿泠", first_ordinal=1)
    b = await store.upsert_entity(work["id"], kind="character", name="老周",
                                  canonical="老周", first_ordinal=1)
    await store.link_entity_card(a["id"], card["id"])
    await store.upsert_relation(work["id"], src_id=a["id"], dst_id=b["id"], rel_type="同盟")
    await store.add_event(work["id"], ordinal=1, summary="渡口相遇",
                          participants=[a["id"], b["id"]], story_time="黄昏", importance=2)
    await store.add_state(work["id"], a["id"], 1, '{"status": "受伤"}')
    await store.add_foreshadow(work["id"], planted_ordinal=1, content="半块玉佩")
    return {
        "work": work, "volume": volume, "chapter": chapter, "source": source,
        "card": card, "a": a, "b": b,
    }


async def export_bundle(client, work_id: str) -> dict:
    r = await client.get(f"/studio/works/{work_id}/bundle")
    assert r.status_code == 200, r.text
    return r.json()


async def import_bundle(client, payload: dict):
    return await client.post("/studio/works/import", json=payload)


# ==================== 导出 ====================

async def test_export_carries_tree_versions_and_graph(studio) -> None:
    """导出包含可迁移的全部内容；派生索引不搬。"""
    client, store = studio
    ids = await seed(store)

    bundle = await export_bundle(client, ids["work"]["id"])

    assert bundle["format"] == "novel-studio/work"
    assert bundle["version"] == 1
    assert bundle["work"]["title"] == "迁移测试"
    assert [n["title"] for n in bundle["nodes"]] == ["第一卷", "第一章"]
    assert len(bundle["versions"]) == 1
    assert bundle["versions"][0]["source"] == "human"
    assert bundle["facts"][0]["source_id"] == ids["source"]["id"]
    assert bundle["terms"][0]["term"] == "阿泠"
    assert bundle["cards"][0]["name"] == "阿泠"
    assert {e["name"] for e in bundle["entities"]} == {"阿泠", "老周"}
    assert bundle["relations"][0]["rel_type"] == "同盟"
    assert json.loads(bundle["events"][0]["participants"]) == [ids["a"]["id"], ids["b"]["id"]]
    assert bundle["states"][0]["state_json"] == '{"status": "受伤"}'
    assert bundle["foreshadows"][0]["content"] == "半块玉佩"
    assert "chunks" not in bundle and "runs" not in bundle


async def test_export_unknown_work_is_404(studio) -> None:
    client, _ = studio
    assert (await client.get("/studio/works/w_missing/bundle")).status_code == 404


# ==================== 导入 ====================

async def test_import_remaps_ids_and_keeps_structure(studio) -> None:
    """新书新 id：树、版本、素材、图谱的引用都指向新 id，内容一字不差。"""
    client, store = studio
    ids = await seed(store)
    bundle = await export_bundle(client, ids["work"]["id"])

    r = await import_bundle(client, bundle)

    assert r.status_code == 200, r.text
    got = r.json()["imported"][0]
    new_id = got["id"]
    assert new_id and new_id != ids["work"]["id"]
    assert got["nodes"] == 2 and got["versions"] == 1 and got["entities"] == 2
    # 导入完顺手建了索引：刚导进来的书立刻能搜
    assert got["chunks"] > 0

    tree = {n["title"]: n for n in await store.list_tree(new_id)}
    assert set(tree) == {"第一卷", "第一章"}
    assert tree["第一章"]["parent_id"] == tree["第一卷"]["id"]
    assert tree["第一章"]["ordinal"] == 1
    assert (await store.get_node(tree["第一章"]["id"]))["content"] == "阿泠走进渡口，老周在等。"

    versions = await store.list_versions(tree["第一章"]["id"])
    assert len(versions) == 1 and versions[0]["source"] == "human"

    entities = {e["name"]: e for e in await store.list_entities(new_id)}
    assert set(entities) == {"阿泠", "老周"}
    assert entities["阿泠"]["id"] != ids["a"]["id"]
    cards = await store.list_cards(new_id)
    assert entities["阿泠"]["card_id"] == cards[0]["id"]

    events = await store.list_events(new_id)
    assert [json.loads(e["participants"]) for e in events] == [
        [entities["阿泠"]["id"], entities["老周"]["id"]]]
    assert [s["entity_id"] for s in await store.list_states(new_id)] == [entities["阿泠"]["id"]]

    sources = await store.list_sources(new_id)
    facts = await store.list_facts(new_id)
    assert sources[0]["id"] != ids["source"]["id"]
    assert facts[0]["source_id"] == sources[0]["id"]


async def test_import_twice_creates_two_independent_books(studio) -> None:
    """同一份包导两次是两本书：id 不撞、互不影响。"""
    client, store = studio
    ids = await seed(store)
    bundle = await export_bundle(client, ids["work"]["id"])

    first = (await import_bundle(client, bundle)).json()["imported"][0]
    second = (await import_bundle(client, bundle)).json()["imported"][0]

    assert first["id"] != second["id"]
    first_tree = {n["title"]: n["id"] for n in await store.list_tree(first["id"])}
    second_tree = {n["title"]: n["id"] for n in await store.list_tree(second["id"])}
    assert set(first_tree.values()).isdisjoint(second_tree.values())


async def test_import_rejects_foreign_or_broken_bundles(studio) -> None:
    """format/版本/标题任一不对就 400，不落半本书。"""
    client, store = studio
    await seed(store)

    assert (await import_bundle(client, {"format": "something-else"})).status_code == 400

    bundle = await export_bundle(client, (await store.list_works())[0]["id"])
    bad_version = {**bundle, "version": 99}
    assert (await import_bundle(client, bad_version)).status_code == 400

    no_title = {**bundle, "work": {**bundle["work"], "title": "  "}}
    r = await import_bundle(client, no_title)
    assert r.status_code == 400
    assert "标题" in r.json()["detail"]


# ==================== 整库包 ====================

async def test_library_export_and_import(studio) -> None:
    """整库导出是一层书目；导入时一本坏的不拦好的，失败清单一起回来。"""
    client, store = studio
    ids = await seed(store)
    other = await store.create_work(kind="article", title="另一篇")
    await store.create_node(work_id=other["id"], kind="section", title="开头", content="正文")

    r = await client.get("/studio/library/bundle")
    assert r.status_code == 200
    library = r.json()
    assert library["format"] == "novel-studio/library"
    assert {w["work"]["title"] for w in library["works"]} == {"迁移测试", "另一篇"}

    good = next(w for w in library["works"] if w["work"]["title"] == "迁移测试")
    broken = {**good, "work": {**good["work"], "title": ""}}
    r = await import_bundle(client, {**library, "works": [broken, good]})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["failed"] and body["failed"][0]["index"] == 1
    works = await store.list_works()
    assert [w["title"] for w in works].count("迁移测试") == 2
    assert [w["title"] for w in works].count("另一篇") == 1
    assert ids["work"]["id"] in {w["id"] for w in works}
