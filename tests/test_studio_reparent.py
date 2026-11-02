"""跨卷拖拽：把一章拖进另一卷末尾，全书章号跟着树序重排。

同层上下移（`move_node`）只交换两个章号，`reparent_node` 不一样：章节换了卷，
树上的位置与全书的次序都变了，但「章号」在库里不止 nodes 一处——摘要、分块、
图谱的时间线（`entities.first_ordinal` / `status_ordinal`）、事件顺序、人物状态、
伏笔超期、关系与证据，各自存了一份数字。不跟着重排的话，「她第 2 章死了」
会留在第 1 章的位置上，一致性检查照着章号比对，拖一次就错位。

重排本身也有坑：`entity_states UNIQUE(entity_id, ordinal)` 与
`relations UNIQUE(src_id, rel_type, dst_id, since_ordinal)` 让逐行直接写变成
中间态碰撞，只能先整体取负、再翻成新号（`_renumber_locked`）。

这一轮的口径：只搬有 ordinal 的叶子，目标是同作品的容器或顶层，落点只有
「目标末尾」；卷不动、不插到某一节之前。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import indexer
from app.studio.store import StudioStore


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def two_volumes(store) -> dict:
    """两卷四章：第一卷 A1/A2，第二卷 B1/B2；章号按树序 1..4。

    正文都建好索引、各有一份节摘要，方便断言「章号副本」跟着走。
    """
    work = await store.create_work(kind="novel", title="跨卷测试", scale="series")
    wid = work["id"]
    v1 = await store.create_node(work_id=wid, kind="volume", title="第一卷")
    v2 = await store.create_node(work_id=wid, kind="volume", title="第二卷")
    nodes = {}
    for i, (title, parent, key) in enumerate([
        ("A1", v1, "a1"), ("A2", v1, "a2"), ("B1", v2, "b1"), ("B2", v2, "b2"),
    ], 1):
        node = await store.create_node(
            work_id=wid, kind="chapter", parent_id=parent["id"],
            title=title, content=f"{title}的正文：灯还亮着。" * 30, ordinal=i,
        )
        assert await indexer.index_node(store, await store.get_node(node["id"])) > 0
        await store.upsert_summary(
            wid, scope="node", ref_id=node["id"], content=f"【{title}】事件",
            ref_ordinal=i, ref_hash=node["content_hash"],
        )
        nodes[key] = node
    return {"work_id": wid, "v1": v1, "v2": v2, **nodes}


async def chapter_ordinals(store, work_id: str) -> dict[str, int]:
    return {n["title"]: int(n["ordinal"]) for n in await store.list_readable(work_id)}


async def chunk_ordinal(store, node_id: str) -> list[int]:
    async with store._lock:
        cur = await store._db.execute(
            "SELECT DISTINCT ordinal FROM chunks WHERE node_id = ? ORDER BY ordinal", (node_id,))
        return [r["ordinal"] for r in await cur.fetchall()]


# ==================== 搬到另一卷 ====================

async def test_move_to_another_volume_lands_at_its_end(store) -> None:
    """A1 拖进第二卷：parent / path / 树序都落到第二卷最后，自身还是那一节。"""
    f = await two_volumes(store)

    r = await store.reparent_node(f["a1"]["id"], f["v2"]["id"])

    assert r["moved"] is True and r["renumbered"] is True
    assert r["parent_title"] == "第二卷"
    rows = {n["id"]: n for n in await store.list_tree(f["work_id"])}
    assert rows[f["a1"]["id"]]["parent_id"] == f["v2"]["id"]
    assert rows[f["a1"]["id"]]["depth"] == 1
    assert rows[f["a1"]["id"]]["path"] == f"{f['v2']['path']}.0003", "落在 B1/B2 之后"
    assert [n["title"] for n in await store.list_tree(f["work_id"])] == [
        "第一卷", "A2", "第二卷", "B1", "B2", "A1"]
    assert r["ordinal"] == 4, "A1 现在全书最后"


async def test_ordinals_are_renumbered_along_the_tree(store) -> None:
    """重排后全书章号是 1..N，且与 `ORDER BY path` 的树序一一对应。"""
    f = await two_volumes(store)

    await store.reparent_node(f["a1"]["id"], f["v2"]["id"])

    tree = [n["title"] for n in await store.list_tree(f["work_id"]) if n["ordinal"] is not None]
    assert tree == ["A2", "B1", "B2", "A1"]
    readable = [(n["title"], int(n["ordinal"])) for n in await store.list_readable(f["work_id"])]
    assert readable == [(title, i) for i, title in enumerate(tree, 1)]


async def test_summaries_and_chunks_follow_the_new_ordinals(store) -> None:
    """摘要与分块按节点 id 精确改：内容留在原来那一节上，章号跟着树走。"""
    f = await two_volumes(store)

    await store.reparent_node(f["a1"]["id"], f["v2"]["id"])

    sums = {s["ref_id"]: int(s["ref_ordinal"])
            for s in await store.list_summaries(f["work_id"], "node")}
    assert sums == {
        f["a2"]["id"]: 1, f["b1"]["id"]: 2, f["b2"]["id"]: 3, f["a1"]["id"]: 4,
    }
    assert await chunk_ordinal(store, f["a1"]["id"]) == [4]
    assert await chunk_ordinal(store, f["a2"]["id"]) == [1]


async def test_graph_columns_are_remapped_without_unique_collisions(store) -> None:
    """图谱十列按旧→新映射；构造一组「直接写就撞 UNIQUE」的值验证取负两步法。

    A1（旧号 1）→ 4、B2（旧号 4）→ 3：同实体的两条状态、同一对关系的两个
    since_ordinal 正好交叉，逐行先写 1→4 就会撞上已经存在的 4 与 3。
    """
    f = await two_volumes(store)
    wid = f["work_id"]
    e = await store.upsert_entity(wid, kind="character", name="阿泠", canonical="阿泠",
                                  first_ordinal=4)
    other = await store.upsert_entity(wid, kind="character", name="老周", canonical="老周",
                                      first_ordinal=1)
    await store.set_entity_status(e["id"], "dead", 4)
    await store.add_state(wid, e["id"], 1, '{"status":"出场"}')
    await store.add_state(wid, e["id"], 4, '{"status":"死去"}')
    await store.upsert_relation(wid, src_id=e["id"], dst_id=other["id"], rel_type="敌对",
                                since_ordinal=1, evidence_ordinal=4)
    await store.upsert_relation(wid, src_id=e["id"], dst_id=other["id"], rel_type="旧识",
                                since_ordinal=4, evidence_ordinal=1)
    await store.add_event(wid, ordinal=1, summary="A1 的事", participants=[])
    await store.add_foreshadow(wid, planted_ordinal=4, content="B2 埋的线")

    await store.reparent_node(f["a1"]["id"], f["v2"]["id"])

    states = {(s["entity_id"], int(s["ordinal"])): s["state_json"]
              for s in await store.list_states(wid)}
    assert states == {
        (e["id"], 4): '{"status":"出场"}',
        (e["id"], 3): '{"status":"死去"}',
    }, "1→4、4→3，两条都在（没有撞 UNIQUE(entity_id, ordinal)）"
    rels = {(r["rel_type"], int(r["since_ordinal"])): int(r["evidence_ordinal"])
            for r in await store.list_relations(wid)}
    assert rels == {("敌对", 4): 3, ("旧识", 3): 4}
    assert (await store.get_entity_by_canonical(wid, "阿泠"))["first_ordinal"] == 3
    assert (await store.get_entity_by_canonical(wid, "老周"))["first_ordinal"] == 4
    assert (await store.get_entity_by_canonical(wid, "阿泠"))["status_ordinal"] == 3
    assert {e["summary"]: int(e["ordinal"]) for e in await store.list_events(wid)} == {"A1 的事": 4}
    assert (await store.list_foreshadows(wid))[0]["planted_ordinal"] == 3


# ==================== 不动的分支 ====================

async def test_already_at_the_end_changes_nothing(store) -> None:
    """B2 本来就在第二卷末尾、全书号也连续：返回 moved False，一行都不许改。"""
    f = await two_volumes(store)
    before = [(n["id"], n["ordinal"], n["path"]) for n in await store.list_tree(f["work_id"])]
    states_before = await store.list_states(f["work_id"])

    r = await store.reparent_node(f["b2"]["id"], f["v2"]["id"])

    assert r == {"moved": False, "reason": "已经在这一卷末尾"}
    assert [(n["id"], n["ordinal"], n["path"]) for n in await store.list_tree(f["work_id"])] == before
    assert await store.list_states(f["work_id"]) == states_before


# ==================== 拒绝的情况 ====================

async def test_leaf_cross_work_and_self_are_refused(store) -> None:
    """目标是叶子 / 跨作品 / 目标是自己都是误拖：一律 ValueError。"""
    f = await two_volumes(store)
    other_work = await store.create_work(kind="novel", title="另一本", scale="series")
    other_vol = await store.create_node(
        work_id=other_work["id"], kind="volume", title="别卷")

    with pytest.raises(ValueError, match="目标不是容器"):
        await store.reparent_node(f["a1"]["id"], f["a2"]["id"])
    with pytest.raises(ValueError, match="跨作品"):
        await store.reparent_node(f["a1"]["id"], other_vol["id"])
    with pytest.raises(ValueError, match="拖进它自己"):
        await store.reparent_node(f["a1"]["id"], f["a1"]["id"])
    # 拒绝要彻底：树与章号保持原样
    assert [n["title"] for n in await store.list_tree(f["work_id"])] == [
        "第一卷", "A1", "A2", "第二卷", "B1", "B2"]
    assert await chapter_ordinals(store, f["work_id"]) == {
        "A1": 1, "A2": 2, "B1": 3, "B2": 4}


async def test_container_source_and_unknown_node(store) -> None:
    """容器不能当拖拽源；未知节点/目标返回 None（路由层 404）。"""
    f = await two_volumes(store)

    with pytest.raises(ValueError, match="容器节点不能拖动"):
        await store.reparent_node(f["v1"]["id"], f["v2"]["id"])
    assert await store.reparent_node("n_不存在", f["v2"]["id"]) is None
    assert await store.reparent_node(f["a1"]["id"], "n_不存在") is None


# ==================== 接口层 ====================

def test_reparent_route_maps_404_and_400(tmp_path) -> None:
    """`POST /studio/nodes/{id}/reparent`：误拖 400 带人话，未知节点 404。"""
    store = StudioStore(tmp_path / "studio.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.include_router(studio_routes.router)

    with TestClient(app) as client:
        work = client.post("/studio/works", json={"kind": "novel", "title": "路由拖拽",
                                                 "scale": "series"}).json()
        vol = client.post(f"/studio/works/{work['id']}/nodes",
                          json={"kind": "volume", "title": "第一卷"}).json()
        ids = [client.post(f"/studio/works/{work['id']}/nodes",
                           json={"kind": "chapter", "title": f"第{i}章",
                                 "content": f"内容{i}" * 60}).json()["id"] for i in (1, 2)]

        r = client.post(f"/studio/nodes/{ids[1]}/reparent", json={"parent_id": vol["id"]})
        assert r.status_code == 200
        assert r.json() == {"moved": True, "ordinal": 1, "parent_title": "第一卷",
                            "renumbered": True}
        assert [n["title"] for n in client.get(f"/studio/works/{work['id']}/tree").json()] == [
            "第一卷", "第2章", "第1章"]

        assert client.post(f"/studio/nodes/{ids[0]}/reparent",
                           json={"parent_id": ids[1]}).status_code == 400
        assert client.post("/studio/nodes/n_不存在/reparent",
                           json={"parent_id": vol["id"]}).status_code == 404
        # 拖回顶层也是合法落点：章 2 从卷里出来，落到顶层末尾
        assert client.post(f"/studio/nodes/{ids[1]}/reparent",
                           json={"parent_id": None}).json()["moved"] is True
