"""迭代 A：计划、设定卡、伏笔、实体与告警备注都该是可编辑的。

这些数据的共同点是「模型给的只是草稿，作者才是权威」：
- 每节的 prompt / 目标字数原来只活在 `works.meta_json.outline`，右栏看不见也改不了；
- 设定卡只能加和删，改一个字就得整张重建；
- 伏笔、实体、告警都是只读，作者判断过的东西下次还会再来一遍。

用例覆盖三层：节点计划读写与提示词优先级、图谱编辑接口、作品包里的计划字段。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import studio_routes
from app.studio import prompting
from app.studio.store import StudioStore


class FakeGateway:
    """不调模型：`complete` 固定返回一张节拍卡，够验证写回与格式清洗。"""

    def __init__(self) -> None:
        self.last_prompt = ""

    def resolve(self, model: str):
        return ("fake", "")

    async def complete(self, *, model: str, system: str, prompt: str, mode: str = "quick", **_kw):
        self.last_prompt = prompt
        return SimpleNamespace(
            text="出场：林昭、老周\n地点：渡口\n事件：\n- 林昭捡到半块玉佩\n"
                 "- 老周认出玉佩\n变化：林昭知道父亲来过这里\n钩子：有人在暗处看着"
        )


class FakeRegistry:
    """`pick_model` 只要求能 route；测试里不关心选到哪个模型。"""

    adapters: dict = {}

    def route(self, name: str):
        return object()


@pytest.fixture
async def studio(tmp_path):
    """同一事件循环里起 store 与 ASGI 客户端：aiosqlite 的连接不能跨循环用。"""
    store = StudioStore(tmp_path / "studio.db")
    await store.init()
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.state.gateway = FakeGateway()
    app.state.registry = FakeRegistry()
    app.include_router(studio_routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store, app
    await store.close()


async def make_novel(store) -> tuple[dict, dict]:
    work = await store.create_work(kind="novel", title="计划测试", scale="series")
    node = await store.create_node(
        work_id=work["id"], kind="chapter", title="第一章", content="林昭走进渡口。" * 30)
    return work, node


# ==================== 计划 ====================

async def test_plan_roundtrip_and_prompt_precedence(studio) -> None:
    """计划能存能读，且生成指令优先用节点计划，不再被旧大纲盖住。"""
    client, store, _ = studio
    work, node = await make_novel(store)
    await store.update_work(work["id"], meta_json=json.dumps({
        "outline": {node["id"]: {"prompt": "旧大纲要点", "target_words": 800}},
    }, ensure_ascii=False))

    r = await client.patch(f"/studio/nodes/{node['id']}", json={
        "plan_json": {
            "prompt": "林昭在渡口捡到玉佩，老周认出它",
            "target_words": 1200,
            "pov": "第三人称限知，跟林昭",
            "time": "入冬后第三天",
            "place": "渡口",
        },
    })

    assert r.status_code == 200, r.text
    got = r.json()
    plan = json.loads(got["plan_json"])
    assert plan["prompt"].startswith("林昭在渡口")
    assert plan["target_words"] == 1200
    assert plan["pov"].startswith("第三人称")

    # 旧大纲也同步了：导出包、打分与还没升级的读法都看那一份
    saved_work = await store.get_work(work["id"])
    outline = json.loads(saved_work["meta_json"])["outline"]
    assert outline[node["id"]]["prompt"].startswith("林昭在渡口")

    text = prompting.node_instruction(got, {})
    assert "林昭在渡口" in text
    assert "旧大纲要点" not in text
    assert "1200 字" in text
    assert "第三人称限知" in text
    assert "入冬后第三天" in text
    assert "渡口" in text


async def test_plan_falls_back_to_legacy_outline(studio) -> None:
    """没保存过计划的老节点，仍然读得到 meta_json.outline 里的要点。"""
    _client, store, _ = studio
    work, node = await make_novel(store)
    meta = {"outline": {node["id"]: {"prompt": "旧版大纲", "target_words": 900}}}
    fresh = await store.get_node(node["id"])

    text = prompting.node_instruction(fresh, meta)

    assert "旧版大纲" in text
    assert "900 字" in text


async def test_beats_endpoint_writes_summary(studio) -> None:
    """交互式重抽节拍卡：走同一套 prompt，结果写回 nodes.summary。"""
    client, store, app = studio
    _work, node = await make_novel(store)

    r = await client.post(f"/studio/nodes/{node['id']}/beats", json={
        "focus": "林昭第一次见到老周",
    })

    assert r.status_code == 200, r.text
    beats = r.json()["beats"]
    assert "出场" in beats and "钩子" in beats
    assert "林昭第一次见到老周" in app.state.gateway.last_prompt
    fresh = await store.get_node(node["id"])
    assert fresh["summary"] == beats


# ==================== 设定卡 / 伏笔 / 实体 ====================

async def test_card_can_be_edited_in_place(studio) -> None:
    """带 id 提交就是改卡，不是再建一张；其他 data_json 字段不能丢。"""
    client, store, _ = studio
    work, _node = await make_novel(store)

    r = await client.post(f"/studio/works/{work['id']}/cards", json={
        "kind": "character", "name": "林昭", "summary": "刀客",
        "data_json": json.dumps({"rank": "筑基", "status": "alive"}, ensure_ascii=False),
    })
    card = r.json()

    r2 = await client.post(f"/studio/works/{work['id']}/cards", json={
        "id": card["id"], "kind": "character", "name": "林昭", "summary": "左手刀客",
        "aliases": ["阿昭"],
        "data_json": json.dumps({"rank": "金丹", "status": "dead"}, ensure_ascii=False),
    })

    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == card["id"]
    cards = await store.list_cards(work["id"])
    assert len(cards) == 1
    assert cards[0]["summary"] == "左手刀客"
    assert json.loads(cards[0]["aliases"]) == ["阿昭"]
    data = json.loads(cards[0]["data_json"])
    assert data == {"rank": "金丹", "status": "dead"}


async def test_foreshadow_can_be_resolved_or_abandoned(studio) -> None:
    """抽出来的伏笔不一定都算数：作者能回收，也能明确弃用。"""
    client, store, _ = studio
    work, _node = await make_novel(store)
    first = await store.add_foreshadow(
        work["id"], planted_ordinal=1, content="半块玉佩")
    second = await store.add_foreshadow(
        work["id"], planted_ordinal=2, content="墙上的旧剑")

    r = await client.patch(f"/studio/foreshadows/{first}", json={
        "status": "resolved", "ordinal": 3})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"
    assert r.json()["resolved_ordinal"] == 3

    r2 = await client.patch(f"/studio/foreshadows/{second}", json={"status": "abandoned"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "abandoned"

    rows = {f["id"]: f for f in await store.list_foreshadows(work["id"])}
    assert rows[first]["status"] == "resolved"
    assert rows[second]["status"] == "abandoned"


async def test_entity_update_and_canonical_conflict(studio) -> None:
    """实体改名 / 补别名 / 改状态；撞上已有 canonical 时报 400，不静默合并。"""
    client, store, _ = studio
    work, _node = await make_novel(store)
    lin = await store.upsert_entity(
        work["id"], kind="character", name="林昭", canonical="林昭")
    zhou = await store.upsert_entity(
        work["id"], kind="character", name="周叙", canonical="周叙")

    r = await client.patch(f"/studio/entities/{lin['id']}", json={
        "name": "林昭", "kind": "character", "aliases": ["阿昭", "林公子"],
        "status": "dead", "status_ordinal": 5,
    })

    assert r.status_code == 200, r.text
    row = r.json()
    assert json.loads(row["aliases"]) == ["阿昭", "林公子"]
    assert row["status"] == "dead" and row["status_ordinal"] == 5

    conflict = await client.patch(f"/studio/entities/{lin['id']}", json={"name": "周叙"})
    assert conflict.status_code == 400
    assert "同名实体" in conflict.json()["detail"]
    assert (await store.get_entity(zhou["id"]))["name"] == "周叙"


async def test_entity_rename_syncs_linked_card_name(studio) -> None:
    """实体挂着作者手写的设定卡时，改名要跟着改卡名。

    卡是抽取时的权威提示；只改实体不改卡，下一次抽取会把旧名再当成一个人，
    图谱里就多出一个「林昭」。
    """
    client, store, _ = studio
    work, _node = await make_novel(store)
    card = await store.upsert_card(
        work["id"], kind="character", name="林昭", summary="刀客")
    entity = await store.upsert_entity(
        work["id"], kind="character", name="林昭", canonical="林昭")
    await store.link_entity_card(entity["id"], card["id"])

    r = await client.patch(f"/studio/entities/{entity['id']}", json={"name": "林照"})

    assert r.status_code == 200, r.text
    cards = await store.list_cards(work["id"])
    assert cards[0]["name"] == "林照"
    assert cards[0]["summary"] == "刀客"


# ==================== 告警备注 / 作品包 ====================

async def test_issue_resolution_note_is_persisted(studio) -> None:
    """忽略告警要留下原因：同一条下次再出现，人不用从头想一遍。"""
    client, store, _ = studio
    work, node = await make_novel(store)
    issue = await store.add_issue(
        work["id"], rule="dead_character", message="林昭已经死了，这一节又说话",
        node_id=node["id"], severity="error")

    r = await client.post(f"/studio/issues/{issue['id']}/resolve", json={
        "note": "这是回忆段落，不是复活"})

    assert r.status_code == 200, r.text
    rows = await store.list_issues(work["id"], only_open=False)
    assert rows[0]["resolved"] == 1
    assert rows[0]["resolution_note"] == "这是回忆段落，不是复活"


async def test_bundle_carries_node_plan(studio) -> None:
    """计划是作者写的，不属于可重建的派生数据，作品包必须带上。"""
    _client, store, _ = studio
    work, node = await make_novel(store)
    await store.update_node(node["id"], plan_json=json.dumps({
        "prompt": "本节要推进玉佩线索", "target_words": 1500, "place": "渡口",
    }, ensure_ascii=False))

    bundle = await store.export_work(work["id"])
    assert bundle is not None
    exported = next(n for n in bundle["nodes"] if n["id"] == node["id"])
    assert json.loads(exported["plan_json"])["target_words"] == 1500

    got = await store.import_work(bundle)
    tree = await store.list_tree(got["id"])
    new_node = await store.get_node(tree[0]["id"])
    plan = json.loads(new_node["plan_json"])
    assert plan["prompt"] == "本节要推进玉佩线索"
    assert plan["place"] == "渡口"
