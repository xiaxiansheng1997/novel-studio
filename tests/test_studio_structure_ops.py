"""迭代 B 结构操作：duplicate / split / merge 的回归测试。

三条口径与 reparent（`test_studio_reparent.py`）完全一致：

1. **章号重排复用 `_renumber_locked`**——树上位置变了之后，全书可读单元必须
   还是按树序 1..N 连续，且摘要、分块这些「副本章号」跟着走；
2. **派生数据失效**——被删/被吸收节的 chunks、chunks_fts、index_units、
   summaries 由 `_drop_unit_rows` 清干净（不然 S4 上下文里还魂）；上游卷/全书
   摘要被标脏；库标脏（`index_state.dirty`）让下一次重建知道成员动过；
3. **备份优先**——合并本质是一次删除，被吸收的节走 `delete_node` 同一条
   备份路；备份写不成要整体回滚，不能留下「正文已拼好、原节没备份」的半截。

insert-before 由 `create + reparent(before_id)` 组合完成，重排逻辑在
`test_studio_reparent.py` 已覆盖，这里只测路由层能走通。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import indexer
from app.studio.store import BackupError, StudioStore


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def chain(store, titles: list[str], work_title: str = "构造测试") -> dict:
    """一卷 N 章：每章都建好索引、各有一份节摘要，方便断言派生数据跟着走。"""
    work = await store.create_work(kind="novel", title=work_title, scale="series")
    wid = work["id"]
    vol = await store.create_node(work_id=wid, kind="volume", title="第一卷")
    out: dict = {"work_id": wid, "vol": vol}
    for i, t in enumerate(titles, 1):
        node = await store.create_node(
            work_id=wid, kind="chapter", parent_id=vol["id"],
            title=t, content=f"{t}的正文。" * 20, ordinal=i,
        )
        assert await indexer.index_node(store, await store.get_node(node["id"])) > 0
        await store.upsert_summary(
            wid, scope="node", ref_id=node["id"], content=f"【{t}】事件",
            ref_ordinal=i, ref_hash=node["content_hash"],
        )
        out[t] = node
    return out


async def readable(store, work_id: str) -> list[tuple[str, int]]:
    return [(n["title"], int(n["ordinal"])) for n in await store.list_readable(work_id)]


async def chunk_count(store, node_id: str) -> int:
    async with store._lock:
        cur = await store._db.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE node_id = ?", (node_id,))
        return int((await cur.fetchone())["c"])


async def summary_refs(store, work_id: str) -> dict[str, int]:
    return {s["ref_id"]: int(s["ref_ordinal"])
            for s in await store.list_summaries(work_id, "node")}


async def set_node_summary(store, node_id: str, text: str) -> None:
    """直写节拍卡：create_node 不收 summary 参数，测试里也没必要为它开接口。"""
    async with store._lock:
        await store._db.execute("UPDATE nodes SET summary = ? WHERE id = ?", (text, node_id))
        await store._db.commit()


# ==================== duplicate ====================

async def test_duplicate_lands_right_after_the_original(store) -> None:
    """复制一节：紧挨原节之后、内容一致、全书章号重排连续。"""
    f = await chain(store, ["灯下", "雨中"])

    r = await store.duplicate_node(f["灯下"]["id"])

    assert r["ok"] is True and r["copied"] == 1 and r["renumbered"] is True
    clone = await store.get_node(r["new_node_id"])
    assert clone["title"] == "灯下" and clone["content"] == f["灯下"]["content"]
    assert clone["parent_id"] == f["vol"]["id"] and clone["ordinal"] is not None
    assert await readable(store, f["work_id"]) == [("灯下", 1), ("灯下", 2), ("雨中", 3)]

    # 派生数据不跟壳复制：新节没有摘要行、没有分块
    refs = await summary_refs(store, f["work_id"])
    assert clone["id"] not in refs
    assert refs == {f["灯下"]["id"]: 1, f["雨中"]["id"]: 3}, "原节的摘要跟着新章号走"
    assert await chunk_count(store, clone["id"]) == 0
    assert await chunk_count(store, f["灯下"]["id"]) > 0


async def test_duplicate_copies_whole_subtree(store) -> None:
    """章带节时连子树一起复制：克隆的节挂在克隆的章下面，树序原样镜像。"""
    work = await store.create_work(kind="novel", title="子树复制", scale="series")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    ch = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                 title="章", content="章本体" * 20, ordinal=1)
    s1 = await store.create_node(work_id=work["id"], kind="section", parent_id=ch["id"],
                                 title="节一", content="第一节字" * 20, ordinal=2)
    s2 = await store.create_node(work_id=work["id"], kind="section", parent_id=ch["id"],
                                 title="节二", content="第二节字" * 20, ordinal=3)

    r = await store.duplicate_node(ch["id"])

    assert r["copied"] == 3
    tree = await store.list_tree(work["id"])
    assert [n["title"] for n in tree] == ["第一卷", "章", "节一", "节二", "章", "节一", "节二"]
    clone_root = await store.get_node(r["new_node_id"])
    clones = [n for n in tree if n["parent_id"] == clone_root["id"]]
    assert [n["title"] for n in clones] == ["节一", "节二"]
    assert all(str(n["path"]).startswith(str(clone_root["path"])) for n in clones)
    # list_tree 不带 content：逐节拿全文比对镜像是否一字不差
    for clone, src in zip(clones, [s1, s2]):
        full = await store.get_node(clone["id"])
        assert full["content"] == (await store.get_node(src["id"]))["content"]
    ords = [int(n["ordinal"]) for n in await store.list_readable(work["id"])]
    assert ords == [1, 2, 3, 4, 5, 6], "六节全书连续编号"


async def test_duplicate_unknown_returns_none(store) -> None:
    assert await store.duplicate_node("n_不存在") is None


# ==================== split ====================

async def test_split_at_offset_moves_tail_into_next_sibling(store) -> None:
    """按字符偏移切：原节留第一段，切出的段成为紧挨其后的兄弟节。"""
    f = await chain(store, ["灯下", "雨中"])
    content = f["灯下"]["content"]
    mid = len(content) // 2
    await set_node_summary(store, f["灯下"]["id"], "整节的节拍卡")

    r = await store.split_node(f["灯下"]["id"], [mid])

    assert r["ok"] is True and r["split"] is True and len(r["parts"]) == 2
    first = await store.get_node(f["灯下"]["id"])
    assert first["content"] == content[:mid]
    assert first["summary"] == "", "节拍卡写的是整节的意思，猜一半是编数据"
    second = await store.get_node(r["parts"][1])
    assert second["content"] == content[mid:]
    assert second["title"] == "灯下（2）", "标题不以数字结尾 → 括号后缀"
    assert second["parent_id"] == f["vol"]["id"] and second["kind"] == "chapter"
    assert await readable(store, f["work_id"]) == [("灯下", 1), ("灯下（2）", 2), ("雨中", 3)]


async def test_split_two_offsets_makes_three_parts(store) -> None:
    f = await chain(store, ["灯下"])
    content = f["灯下"]["content"]
    a, b = len(content) // 3, len(content) * 2 // 3

    r = await store.split_node(f["灯下"]["id"], [b, a])  # 乱序给进去也要按位置切

    got = await readable(store, f["work_id"])
    assert [t for t, _ in got] == ["灯下", "灯下（2）", "灯下（3）"]
    assert [o for _, o in got] == [1, 2, 3]
    bodies = [(await store.get_node(nid))["content"] for nid in r["parts"]]
    assert "".join(bodies) == content, "切完拼回去必须一字不差"


async def test_split_upstream_summaries_go_stale(store) -> None:
    """拆过的节上游（卷/全书）摘要标脏：assembler 不能再拿旧卷摘要当真。"""
    f = await chain(store, ["灯下"])
    await store.upsert_summary(f["work_id"], scope="container", ref_id=f["vol"]["id"],
                               content="卷摘要", ref_ordinal=1, ref_hash="新鲜")

    await store.split_node(f["灯下"]["id"], [10])

    sums = await store.list_summaries(f["work_id"], "container")
    assert {s["ref_hash"] for s in sums} == {""}


async def test_split_no_offsets_is_a_noop(store) -> None:
    """前端「没把光标放正文里就点拆分」的兜底：split False，一行都不改。"""
    f = await chain(store, ["灯下", "雨中"])
    before = [(n["id"], n["path"], n["ordinal"]) for n in await store.list_tree(f["work_id"])]

    r = await store.split_node(f["灯下"]["id"], [])

    assert r["ok"] is True and r["split"] is False
    assert [(n["id"], n["path"], n["ordinal"]) for n in await store.list_tree(f["work_id"])] == before


async def test_split_rejects_out_of_range_and_children(store) -> None:
    f = await chain(store, ["灯下", "雨中"])
    with pytest.raises(ValueError, match="越界"):
        await store.split_node(f["灯下"]["id"], [len(f["灯下"]["content"]) + 5])
    # 有子节点的章切不了：子节跟着谁走没有合理解释
    work2 = await store.create_work(kind="novel", title="拆分拒绝", scale="series")
    vol2 = await store.create_node(work_id=work2["id"], kind="volume", title="卷")
    ch = await store.create_node(work_id=work2["id"], kind="chapter", parent_id=vol2["id"],
                                 title="章", content="章正文" * 20, ordinal=1)
    await store.create_node(work_id=work2["id"], kind="section", parent_id=ch["id"],
                            title="节", content="节正文" * 20, ordinal=2)
    with pytest.raises(ValueError, match="子节点"):
        await store.split_node(ch["id"], [10])
    assert await store.split_node("n_不存在", [10]) is None


# ==================== merge ====================

async def test_merge_absorbs_next_and_backs_it_up(store) -> None:
    """合并 = 正文拼进第一节 + 被吸收节先备份再删：派生行全部清干净。"""
    f = await chain(store, ["灯下", "雨中", "桥上", "门前"])
    c = f["门前"]["content"]

    r = await store.merge_nodes(f["桥上"]["id"], count=2)

    assert r["ok"] is True and r["merged"] == 2
    got = await store.get_node(f["桥上"]["id"])
    assert got["content"] == f["桥上"]["content"] + "\n\n" + c
    assert got["title"] == "桥上／门前"
    assert await store.get_node(f["门前"]["id"]) is None
    assert await readable(store, f["work_id"]) == [
        ("灯下", 1), ("雨中", 2), ("桥上／门前", 3)]

    # 被吸收节的派生数据不留：分块与节摘要都清了
    assert await chunk_count(store, f["门前"]["id"]) == 0
    assert f["门前"]["id"] not in await summary_refs(store, f["work_id"])
    # 备份跟 delete_node 同一条路：删前那一节的正文在盘上找得回
    backs = store.list_node_backups()
    assert any(b["title"] == "门前" for b in backs), f"备份列表里没有「门前」：{backs}"

    # 库标脏：结构动了，下一次重建要知道成员变过
    async with store._lock:
        cur = await store._db.execute(
            "SELECT dirty FROM index_state WHERE work_id = ?", (f["work_id"],))
        assert int((await cur.fetchone())["dirty"]) == 1


async def test_merge_three_at_once(store) -> None:
    f = await chain(store, ["甲", "乙", "丙", "丁"])

    r = await store.merge_nodes(f["甲"]["id"], count=3)

    assert r["merged"] == 3 and r["title"] == "甲／乙／丙"
    assert await readable(store, f["work_id"]) == [("甲／乙／丙", 1), ("丁", 2)]
    assert (await store.get_node(f["甲"]["id"]))["content"].count("\n\n") == 2


async def test_merge_rejects_container_tail_and_unknown(store) -> None:
    f = await chain(store, ["灯下", "雨中"])
    with pytest.raises(ValueError, match="容器不参与合并"):
        await store.merge_nodes(f["vol"]["id"])
    with pytest.raises(ValueError, match="后面没有"):
        await store.merge_nodes(f["雨中"]["id"])
    assert await store.merge_nodes("n_不存在") is None
    # 拒绝要彻底：树与正文原样
    assert await readable(store, f["work_id"]) == [("灯下", 1), ("雨中", 2)]


async def test_merge_backup_failure_rolls_back_everything(store) -> None:
    """备份写不成 → BackupError，正文拼接也必须整体退回（合并的退路与删除同级）。"""
    f = await chain(store, ["灯下", "雨中", "桥上"])

    def boom(bundle):
        raise BackupError("模拟：备份目录写不进")

    store._write_node_backup = boom
    with pytest.raises(BackupError):
        await store.merge_nodes(f["灯下"]["id"])

    # 没提交的那半截事务不能挂在连接里等下一次 commit 顺手带进库
    first = await store.get_node(f["灯下"]["id"])
    assert first["content"] == f["灯下"]["content"] and first["title"] == "灯下"
    assert await store.get_node(f["雨中"]["id"]) is not None
    assert await readable(store, f["work_id"]) == [("灯下", 1), ("雨中", 2), ("桥上", 3)]
    # 后续写入不受在途脏状态污染
    del store._write_node_backup  # 还原实例方法（类上还在）
    assert await store.delete_node(f["桥上"]["id"]) is True
    assert await store.get_node(f["桥上"]["id"]) is None


# ==================== 接口层 ====================

def _build_client(tmp_path):
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
    return TestClient(app)


def test_structure_routes(tmp_path) -> None:
    """/duplicate /split /merge：状态码口径与 reparent 一致（404 缺节点 / 400 误操作）。"""
    with _build_client(tmp_path) as client:
        work = client.post("/studio/works", json={"kind": "novel", "title": "路由结构",
                                                 "scale": "series"}).json()
        vol = client.post(f"/studio/works/{work['id']}/nodes",
                          json={"kind": "volume", "title": "第一卷"}).json()
        ids = [client.post(f"/studio/works/{work['id']}/nodes",
                           json={"kind": "chapter", "title": f"章{i}",
                                 "parent_id": vol["id"],
                                 "content": f"内容{i}" * 60}).json()["id"] for i in (1, 2)]
        wid = work["id"]

        # duplicate：新节插在章1之后
        r = client.post(f"/studio/nodes/{ids[0]}/duplicate")
        assert r.status_code == 200
        dup_id = r.json()["new_node_id"]
        titles = [n["title"] for n in client.get(f"/studio/works/{wid}/tree").json()]
        assert titles == ["第一卷", "章1", "章1", "章2"]

        # split：按偏移切一刀，切出的节紧跟其后
        r = client.post(f"/studio/nodes/{dup_id}/split", json={"offsets": [10]})
        assert r.status_code == 200 and r.json()["split"] is True
        # 越界 400、空 offsets 是合法的「什么都不做」
        assert client.post(f"/studio/nodes/{dup_id}/split",
                           json={"offsets": [10_000]}).status_code == 400
        assert client.post(f"/studio/nodes/{dup_id}/split",
                           json={"offsets": []}).json()["split"] is False

        # merge：把切出来的那一节合回去（「章1」以数字结尾 → 后缀直接追加）
        r = client.post(f"/studio/nodes/{dup_id}/merge", json={"count": 2})
        assert r.status_code == 200
        assert r.json()["title"] == "章1／章12"
        assert client.post(f"/studio/nodes/{ids[1]}/merge",
                           json={"count": 2}).status_code == 400, "末尾没有可吸收的节"
        assert client.post("/studio/nodes/n_不存在/duplicate").status_code == 404
        assert client.post(f"/studio/nodes/{ids[1]}/merge",
                           json={"count": 1}).status_code == 422, "count 下限由 schema 把关"

        # 全书章号始终按树序连续
        ords = [int(n["ordinal"]) for n in client.get(f"/studio/works/{wid}/tree").json()
                if n["ordinal"] is not None]
        assert sorted(ords) == list(range(1, len(ords) + 1))
