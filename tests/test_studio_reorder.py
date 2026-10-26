"""同层上下移：一次交换要带走的东西远不止 `nodes.ordinal`。

章节顺序之前改不了：`update_node` 的白名单只有 title/content/status/summary，
`ordinal` 在建节点时取 `MAX+1` 之后就再没人写过，接口里也没有 move/reorder。

补这一条时最容易漏的是**章号在库里不止 `nodes` 一处**。图谱的时间线
（`entities.first_ordinal` / `status_ordinal`）、事件顺序（`events.ordinal`）、
人物状态（`entity_states.ordinal`）、伏笔超期判定（`foreshadows.*_ordinal`）、
关系与证据（`relations.since_ordinal` / `until_ordinal` / `evidence_ordinal`）
各自存了一份章号，而且这些地方只存数字、不存节点 id，没法跟着 nodes 一起查。
只换 `nodes.ordinal` 的后果是：排完序「她第 2 章死了」会留在第 3 章的位置上，
一致性检查照着章号比对，排一次序就把设定搞乱。

另一处更直观的坑：树上的顺序来自 `list_tree ORDER BY path`，生成顺序来自
`list_readable ORDER BY ordinal`。所以交换必须 path 与 ordinal 一起换——
否则正文的先后已经变了、左侧树却一动不动，两边对不上。
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


async def three_chapters(store) -> tuple[str, list[dict]]:
    """三章的书：正文建好索引、每章一份摘要，卷/全书摘要也各一份（它们的章号是 0）。"""
    work = await store.create_work(kind="novel", title="重排测试")
    nodes = []
    for i in (1, 2, 3):
        content = f"第{i}章的正文：夜色沉下来，灯还亮着。" * 40
        node = await store.create_node(work_id=work["id"], kind="chapter",
                                       title=f"第{i}章", content=content, ordinal=i)
        assert await indexer.index_node(store, await store.get_node(node["id"])) > 0
        await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                                   content=f"【{i}】事件/变化/钩子", ref_ordinal=i,
                                   ref_hash=node["content_hash"])
        nodes.append(node)
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    await store.upsert_summary(work["id"], scope="container", ref_id=vol["id"],
                               content="本卷概要", ref_hash="legacy")
    await store.upsert_summary(work["id"], scope="work", ref_id=work["id"],
                               content="全书概要", ref_hash="legacy")
    return work["id"], nodes


async def chapter_of(store, work_id: str) -> dict[str, int]:
    """标题 → 当前章号（按可读顺序）。"""
    return {n["title"]: int(n["ordinal"]) for n in await store.list_readable(work_id)}


async def chunk_ordinal(store, node_id: str) -> list[int]:
    async with store._lock:
        cur = await store._db.execute(
            "SELECT DISTINCT ordinal FROM chunks WHERE node_id = ? ORDER BY ordinal", (node_id,))
        return [r["ordinal"] for r in await cur.fetchall()]


async def seed_graph(store, work_id: str) -> dict[str, str]:
    """往每张「只存章号」的表各写一笔，用于断言它们跟着位置走。"""
    a = await store.upsert_entity(work_id, kind="character", name="阿泠", canonical="阿泠",
                                  first_ordinal=2)
    b = await store.upsert_entity(work_id, kind="character", name="老周", canonical="老周",
                                  first_ordinal=3)
    await store.set_entity_status(a["id"], "dead", 2)
    await store.add_state(work_id, a["id"], 2, '{"status":"dead"}')
    await store.add_state(work_id, a["id"], 3, '{"status":"回忆出场"}')
    await store.upsert_relation(work_id, src_id=a["id"], dst_id=b["id"], rel_type="敌对",
                               since_ordinal=2, evidence_ordinal=3)
    await store.add_event(work_id, ordinal=2, summary="第二章的事", participants=[])
    await store.add_event(work_id, ordinal=3, summary="第三章的事", participants=[])
    await store.add_foreshadow(work_id, planted_ordinal=3, content="第3章埋的线")
    return {"a": a["id"], "b": b["id"]}


# ==================== 交换的完整性 ====================

async def test_swap_moves_every_copy_of_the_ordinal(store) -> None:
    """第 2 章与第 3 章互换：所有存了章号的地方一起换，一条都不能留在原位。"""
    work_id, nodes = await three_chapters(store)
    await seed_graph(store, work_id)
    await store.touch_index_state(work_id, dirty=0)

    r = await store.move_node(nodes[1]["id"], "down")

    assert r["moved"] is True and r["swapped_with_title"] == "第3章"
    assert await chapter_of(store, work_id) == {"第1章": 1, "第3章": 2, "第2章": 3}

    sums = {s["ref_id"]: s["ref_ordinal"] for s in await store.list_summaries(work_id, "node")}
    assert sums[nodes[1]["id"]] == 3 and sums[nodes[2]["id"]] == 2
    assert await chunk_ordinal(store, nodes[1]["id"]) == [3]

    events = {e["summary"]: e["ordinal"] for e in await store.list_events(work_id)}
    assert events["第二章的事"] == 3 and events["第三章的事"] == 2

    a = await store.get_entity_by_canonical(work_id, "阿泠")
    assert (a["first_ordinal"], a["status_ordinal"]) == (3, 3), "“阿泠第2章出场并死去”现在属于第3章"
    rel = (await store.list_relations(work_id))[0]
    assert (rel["since_ordinal"], rel["evidence_ordinal"]) == (3, 2)
    assert (await store.list_foreshadows(work_id))[0]["planted_ordinal"] == 2


async def test_swap_keeps_tree_and_reading_order_consistent(store) -> None:
    """树上的位置与正文的先后必须一起变：只换 ordinal 的话左侧树会一动不动。"""
    work_id, nodes = await three_chapters(store)
    await store.move_node(nodes[2]["id"], "up")  # 第3章 ↑ 与第2章互换

    tree = [n["title"] for n in await store.list_tree(work_id) if n["ordinal"] is not None]
    readable = [n["title"] for n in await store.list_readable(work_id)]
    assert tree == readable == ["第1章", "第3章", "第2章"]
    # path 也换了位：树是按 path 排的，不然上面的顺序根本不会变
    by_title = {n["title"]: n["path"] for n in await store.list_tree(work_id)}
    assert by_title["第3章"] < by_title["第2章"]


async def test_swap_repaths_the_whole_subtree(store) -> None:
    """带子节点的节参与交换时，子树的 path 必须整段跟着换，层级不能散。"""
    work = await store.create_work(kind="novel", title="带节的书", scale="series")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    c1 = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                 title="第一章", content="一" * 100, ordinal=1)
    c2 = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                 title="第二章", content="二" * 100, ordinal=2)
    s1 = await store.create_node(work_id=work["id"], kind="section", parent_id=c1["id"],
                                 title="第一节", content="三" * 100, ordinal=3)

    assert (await store.move_node(c1["id"], "down"))["moved"] is True

    rows = {n["id"]: n for n in await store.list_tree(work["id"])}
    assert rows[c2["id"]]["path"] == c1["path"], "第二章占了第一章原来的位置"
    assert rows[s1["id"]]["path"].startswith(rows[c1["id"]]["path"] + ".")
    assert rows[s1["id"]]["parent_id"] == c1["id"] and rows[s1["id"]]["depth"] == 2
    # 唯一索引 `UNIQUE(work_id, path)`：树上的顺序现在是 第二章 → 第一章 → 它的第一节
    assert [n["title"] for n in await store.list_tree(work["id"])] == [
        "第一卷", "第二章", "第一章", "第一节"]


async def test_unique_constraints_survive_the_swap(store) -> None:
    """同一实体在第 2、3 章各有一条状态：直接对调会撞 `UNIQUE(entity_id, ordinal)`。

    这就是取负两步法存在的理由——中间态必须既不与正值冲突、彼此也不相同。
    """
    work_id, nodes = await three_chapters(store)
    ids = await seed_graph(store, work_id)
    before = {(s["entity_id"], s["ordinal"]): s["state_json"]
              for s in await store.list_states(work_id)}
    assert len(before) == 2

    await store.move_node(nodes[1]["id"], "down")

    after = {(s["entity_id"], s["ordinal"]): s["state_json"]
             for s in await store.list_states(work_id)}
    assert len(after) == 2, "两条状态都得活着，交换的是章号不是行"
    assert after == {(k[0], 5 - k[1]): v for k, v in before.items()}, "2 ↔ 3，内容留在原来那一章上"
    assert after[(ids["a"], 3)] == '{"status":"dead"}'
    assert after[(ids["a"], 2)] == '{"status":"回忆出场"}'


# ==================== 不重算不该重算的东西 ====================

async def test_swap_does_not_rebuild_index_or_summaries(store) -> None:
    """内容没动：索引台账的哈希戳与摘要的 freshness 都不该变，只把 dirty 置 1。"""
    work_id, nodes = await three_chapters(store)
    units_before = {u["ref_id"]: u["content_hash"] for u in await store.list_index_units(work_id)}
    fresh_before = {s["ref_id"]: s["fresh"] for s in await store.list_summaries(work_id)}

    await store.move_node(nodes[0]["id"], "down")

    assert {u["ref_id"]: u["content_hash"] for u in await store.list_index_units(work_id)} == units_before
    assert {s["ref_id"]: s["fresh"] for s in await store.list_summaries(work_id)} == fresh_before
    assert await store.stale_summaries(work_id) == [], "换位不是改稿，不该把记忆判过期"
    assert int((await store.index_state(work_id))["dirty"]) == 1


async def test_upstream_summaries_keep_their_placeholder_ordinal(store) -> None:
    """卷/全书摘要的 `ref_ordinal` 落的是 0（它们没有单一章号）：交换不能把它们卷进去。"""
    work_id, nodes = await three_chapters(store)
    vol_id = (await store.list_summaries(work_id, "container"))[0]["ref_id"]

    await store.move_node(nodes[2]["id"], "up")

    assert (await store.get_summary("container", vol_id))["ref_ordinal"] == 0
    assert (await store.get_summary("work", work_id))["ref_ordinal"] == 0


# ==================== 边界与不受理的情况 ====================

async def test_boundary_moves_change_nothing(store) -> None:
    """首行上移、末行下移：`moved is False`，库里一切保持原样。"""
    work_id, nodes = await three_chapters(store)
    await seed_graph(store, work_id)
    snapshot = [(n["id"], n["ordinal"], n["path"]) for n in await store.list_tree(work_id)]

    assert await store.move_node(nodes[0]["id"], "up") == {"moved": False, "reason": "已到边界"}
    assert await store.move_node(nodes[2]["id"], "down") == {"moved": False, "reason": "已到边界"}

    assert [(n["id"], n["ordinal"], n["path"]) for n in await store.list_tree(work_id)] == snapshot
    assert await chapter_of(store, work_id) == {"第1章": 1, "第2章": 2, "第3章": 3}
    states = await store.list_states(work_id)
    assert sorted(s["ordinal"] for s in states) == [2, 3], "没有邻居时连章号都不许碰"


async def test_move_never_crosses_parents(store) -> None:
    """跨卷不受理：卷 A 的最后一节与卷 B 的第一节之间「下移」不该把章节搬进另一卷。"""
    work = await store.create_work(kind="novel", title="两卷书", scale="series")
    va = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    vb = await store.create_node(work_id=work["id"], kind="volume", title="第二卷")
    a1 = await store.create_node(work_id=work["id"], kind="chapter", parent_id=va["id"],
                                title="A1", content="甲" * 100, ordinal=1)
    a2 = await store.create_node(work_id=work["id"], kind="chapter", parent_id=va["id"],
                                title="A2", content="乙" * 100, ordinal=2)
    b1 = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vb["id"],
                                title="B1", content="丙" * 100, ordinal=3)

    r = await store.move_node(a2["id"], "down")

    assert r == {"moved": False, "reason": "已到边界"}, "A2 下面还有 B1，但它不在同一个 parent 里"
    rows = {n["id"]: n for n in await store.list_tree(work["id"])}
    assert (rows[a1["id"]]["parent_id"] == va["id"]
            and rows[a2["id"]]["parent_id"] == va["id"]
            and rows[b1["id"]]["parent_id"] == vb["id"])
    assert [n["title"] for n in await store.list_readable(work["id"])] == ["A1", "A2", "B1"]


async def test_container_and_unknown_node_are_refused(store) -> None:
    work_id, nodes = await three_chapters(store)
    vol_id = (await store.list_summaries(work_id, "container"))[0]["ref_id"]

    assert await store.move_node(vol_id, "up") == {"moved": False, "reason": "容器节点不参与排序"}
    assert await store.move_node("n_不存在", "up") == {"moved": False, "reason": "节点不存在"}
    with pytest.raises(ValueError):
        await store.move_node(nodes[0]["id"], "left")


# ==================== 接口层冒烟 ====================

def test_move_route_reports_the_swap(tmp_path) -> None:
    """`POST /studio/nodes/{id}/move`：成功带上换掉的那一节标题；方向非法由 pydantic 拦成 422。

    全程走 HTTP：aiosqlite 的连接绑在建立它的那个事件循环上。
    """
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
        work = client.post("/studio/works", json={"kind": "novel", "title": "路由",
                                                 "scale": "single"}).json()
        ids = [client.post(f"/studio/works/{work['id']}/nodes",
                           json={"kind": "chapter", "title": f"第{i}章",
                                 "content": f"内容{i}" * 60}).json()["id"] for i in (1, 2)]

        r = client.post(f"/studio/nodes/{ids[1]}/move", json={"direction": "up"})
        assert r.status_code == 200
        assert r.json() == {"moved": True, "direction": "up", "ordinal": 1,
                           "swapped_with_title": "第1章", "reason": ""}
        titles = [n["title"] for n in client.get(f"/studio/works/{work['id']}/tree").json()]
        assert titles == ["第2章", "第1章"]

        assert client.post(f"/studio/nodes/{ids[1]}/move",
                           json={"direction": "up"}).json()["moved"] is False
        assert client.post(f"/studio/nodes/{ids[0]}/move",
                           json={"direction": "sideways"}).status_code == 422
