"""批量落节点（导入整本书用的那一条）的回归测试。

为什么有这一条接口：导入原来逐节 `POST /nodes`，实测 2000 章要 39 秒，
而且每节都得重扫一遍同级算 path 序号、单独 commit 一次。这里一个事务
落一批，起点序号只查一次。测试盯的是三件最怕退回去的事：
  1) path / ordinal 得和单节 `create_node` 算出一样的东西（两条路共用同一段插入）；
  2) 一批里坏一节不该让整本导不进来（跳过并报原因），而结构性错误必须整批不落库；
  3) 序号跨到五位时不能撞 `UNIQUE(work_id, path)`——按字符串比大小会。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import MAX_BATCH_SECTIONS, StudioStore


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def new_work(store: StudioStore) -> str:
    work = await store.create_work(kind="novel", title="导入测试", scale="series")
    return str(work["id"])


def secs(n: int, prefix: str = "第") -> list[dict[str, str]]:
    return [{"title": f"{prefix}{i + 1} 章", "content": f"正文 {i + 1}\n\n第二段"} for i in range(n)]


# ==================== 结构正确性 ====================

async def test_batch_paths_and_ordinals_are_contiguous(store: StudioStore) -> None:
    wid = await new_work(store)
    res = await store.create_nodes_batch(work_id=wid, kind="chapter", sections=secs(5))
    assert res["created"] == 5 and res["failed"] == []
    assert res["first_id"].startswith("n_")

    tree = await store.list_tree(wid)
    assert [r["path"] for r in tree] == ["0001", "0002", "0003", "0004", "0005"]
    assert [r["ordinal"] for r in tree] == [1, 2, 3, 4, 5]
    assert {r["kind"] for r in tree} == {"chapter"}
    assert tree[0]["depth"] == 0 and tree[0]["parent_id"] is None


async def test_batch_continues_after_existing_nodes(store: StudioStore) -> None:
    """已有单节建的章，批量接着排：序号不能从头再来（会撞 UNIQUE）。"""
    wid = await new_work(store)
    await store.create_node(work_id=wid, kind="chapter", title="手敲 1", content="甲")
    await store.create_node(work_id=wid, kind="chapter", title="手敲 2", content="乙")
    res = await store.create_nodes_batch(work_id=wid, kind="chapter", sections=secs(3))
    assert res["created"] == 3

    tree = await store.list_tree(wid)
    assert [r["path"] for r in tree] == ["0001", "0002", "0003", "0004", "0005"]
    assert [r["ordinal"] for r in tree] == [1, 2, 3, 4, 5]
    assert [r["title"] for r in tree][:2] == ["手敲 1", "手敲 2"]


async def test_batch_writes_same_fields_as_single_create(store: StudioStore) -> None:
    """两条路必须算出同一套字数与哈希：不然「批量导的章」与「手敲的章」会分家。"""
    wid = await new_work(store)
    body = "灯下写了一行，\n\n又一行。"
    single = await store.create_node(work_id=wid, kind="chapter", title="单节", content=body)
    await store.create_nodes_batch(
        work_id=wid, kind="chapter", sections=[{"title": "批量", "content": body}])
    batch = (await store.list_tree(wid))[1]

    got = await store.get_node(batch["id"])
    assert got["word_count"] == single["word_count"]
    assert got["content_hash"] == single["content_hash"]
    assert got["status"] == "draft" and got["summary"] == ""


async def test_batch_under_parent_keeps_prefix_and_depth(store: StudioStore) -> None:
    wid = await new_work(store)
    vol = await store.create_node(work_id=wid, kind="volume", title="第一卷")
    res = await store.create_nodes_batch(
        work_id=wid, kind="chapter", sections=secs(2), parent_id=vol["id"])
    assert res["created"] == 2

    tree = await store.list_tree(wid)
    ch = [r for r in tree if r["kind"] == "chapter"]
    assert [r["path"] for r in ch] == ["0001.0001", "0001.0002"]
    assert {r["depth"] for r in ch} == {1}
    assert {r["parent_id"] for r in ch} == {vol["id"]}


async def test_container_batch_has_no_ordinal(store: StudioStore) -> None:
    """卷不进全序：ordinal 为 NULL，批量与单节同一口径。"""
    wid = await new_work(store)
    await store.create_nodes_batch(
        work_id=wid, kind="volume", sections=[{"title": "上卷"}, {"title": "下卷"}])
    tree = await store.list_tree(wid)
    assert [r["ordinal"] for r in tree] == [None, None]


async def test_volume_then_chapters_keeps_reading_order(store: StudioStore) -> None:
    """按卷导入的真实顺序：建卷 → 灌章 → 再建下一卷。

    章的全序必须跟着阅读顺序走：生成「上一章摘要」靠 ordinal，
    两卷之间断了号或顺序反了，摘要与连续性检查会拿错章。
    """
    wid = await new_work(store)
    v1 = await store.create_node(work_id=wid, kind="volume", title="第一卷")
    await store.create_nodes_batch(
        work_id=wid, kind="chapter", sections=secs(2), parent_id=v1["id"])
    v2 = await store.create_node(work_id=wid, kind="volume", title="第二卷")
    # 根上的独立章夹在中间：路径排在后面也不该抢走 ordinal
    await store.create_nodes_batch(
        work_id=wid, kind="chapter", sections=[{"title": "尾声", "content": "后来。"}],
        parent_id=v2["id"])

    tree = await store.list_tree(wid)
    assert [r["path"] for r in tree] == [
        "0001", "0001.0001", "0001.0002", "0002", "0002.0001",
    ]
    assert [r["ordinal"] for r in tree] == [None, 1, 2, None, 3]
    assert [r["title"] for r in tree if r["ordinal"]] == ["第1 章", "第2 章", "尾声"]


async def test_next_seq_survives_five_digit_path(store: StudioStore) -> None:
    """path 末段跨到五位时不能算错：按字符串比大小会把 "9999" 当成最大，
    下一个序号退回 9999 再撞 UNIQUE(work_id, path)。"""
    wid = await new_work(store)
    last = await store.create_node(work_id=wid, kind="chapter", title="第九千九百九十九", content="甲")
    assert last["path"] == "0001"
    await store._db.execute("UPDATE nodes SET path = '9999' WHERE id = ?", (last["id"],))  # noqa: SLF001
    await store._db.commit()  # noqa: SLF001

    assert await store._next_seq(wid, None) == 10000  # noqa: SLF001
    nxt = await store.create_node(work_id=wid, kind="chapter", title="第一万", content="乙")
    assert nxt["path"] == "10000"


# ==================== 坏数据与闸 ====================

async def test_batch_skips_blank_title_and_keeps_the_rest(store: StudioStore) -> None:
    wid = await new_work(store)
    res = await store.create_nodes_batch(work_id=wid, kind="chapter", sections=[
        {"title": "第一章", "content": "有正文"},
        {"title": "   ", "content": "标题是空的"},
        {"content": "连标题字段都没有"},
        {"title": "第四章", "content": "有正文"},
    ])
    assert res["created"] == 2
    assert [f["index"] for f in res["failed"]] == [1, 2]
    assert res["failed"][0]["reason"] == "标题为空"
    assert [r["title"] for r in await store.list_tree(wid)] == ["第一章", "第四章"]
    # 空标题被跳过后面的节要顶上号：中间断一个号会让「第几章」对不上
    assert [r["path"] for r in await store.list_tree(wid)] == ["0001", "0002"]
    assert [r["ordinal"] for r in await store.list_tree(wid)] == [1, 2]


async def test_batch_truncates_overlong_title(store: StudioStore) -> None:
    """标题写长了截断而不是整批拒绝：整本导不进来比少两个字的名头严重得多。"""
    wid = await new_work(store)
    res = await store.create_nodes_batch(
        work_id=wid, kind="chapter", sections=[{"title": "长" * 200, "content": "正文"}])
    assert res["created"] == 1 and res["failed"] == []
    assert len((await store.list_tree(wid))[0]["title"]) == 120


async def test_batch_rejects_bad_shapes_without_writing(store: StudioStore) -> None:
    wid = await new_work(store)
    with pytest.raises(ValueError, match="没有要导入的节"):
        await store.create_nodes_batch(work_id=wid, kind="chapter", sections=[])
    with pytest.raises(ValueError, match=f"一次最多 {MAX_BATCH_SECTIONS}"):
        await store.create_nodes_batch(
            work_id=wid, kind="chapter", sections=secs(MAX_BATCH_SECTIONS + 1))
    with pytest.raises(ValueError, match="节点类型不认"):
        await store.create_nodes_batch(work_id=wid, kind="page", sections=secs(2))
    # 父节点不存在：整条都不该落库（批量与单节同一句人话）
    with pytest.raises(ValueError, match="父节点不存在"):
        await store.create_nodes_batch(
            work_id=wid, kind="chapter", sections=secs(2), parent_id="n_missing")
    assert await store.list_tree(wid) == []


async def test_batch_is_one_transaction(store: StudioStore, monkeypatch) -> None:
    """中途出错要整批回滚：落到一半的章节比一个都没导更难解释。"""
    wid = await new_work(store)
    real = StudioStore._insert_node_locked
    state = {"n": 0}

    async def boom(self, **kwargs):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("插到一半炸了")
        return await real(self, **kwargs)

    monkeypatch.setattr(StudioStore, "_insert_node_locked", boom)
    with pytest.raises(RuntimeError):
        await store.create_nodes_batch(work_id=wid, kind="chapter", sections=secs(3))
    assert state["n"] == 2
    assert await store.list_tree(wid) == []


# ==================== 接口层 ====================

def _build_client(tmp_path):
    st = StudioStore(tmp_path / "studio.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = st
    app.include_router(studio_routes.router)
    return TestClient(app)


def test_nodes_batch_route(tmp_path) -> None:
    """/nodes/batch：一次落一批，返回体带 first_id 供前端选中，坏节报原因。"""
    with _build_client(tmp_path) as client:
        wid = client.post("/studio/works", json={
            "kind": "novel", "scale": "series", "title": "夜航船"}).json()["id"]

        r = client.post(f"/studio/works/{wid}/nodes/batch", json={
            "kind": "chapter", "parent_id": None,
            "sections": [{"title": "第一章 灯下", "content": "她推开门，雨还在下。"},
                         {"title": "", "content": "没有标题的一节"}]})
        assert r.status_code == 200
        body = r.json()
        assert body["created"] == 1 and body["first_id"].startswith("n_")
        assert body["failed"] == [{"index": 1, "title": "", "reason": "标题为空"}]

        assert client.get(f"/studio/works/{wid}/tree").json()[0]["title"] == "第一章 灯下"

        bad = client.post(f"/studio/works/{wid}/nodes/batch", json={
            "kind": "chapter", "sections": [{"title": f"第 {i} 节", "content": "x"}
                                            for i in range(MAX_BATCH_SECTIONS + 1)]})
        assert bad.status_code == 400
        assert "分批发" in bad.json()["detail"]

        assert client.post("/studio/works/nope/nodes/batch", json={
            "kind": "chapter", "sections": [{"title": "一", "content": "x"}]}).status_code == 404


def test_batch_import_then_search(tmp_path) -> None:
    """导完建一次索引就能搜到：这是「导入」对下游的承诺（拆解与检索都吃这一口）。"""
    with _build_client(tmp_path) as client:
        wid = client.post("/studio/works", json={
            "kind": "novel", "scale": "series", "title": "search"}).json()["id"]
        r = client.post(f"/studio/works/{wid}/nodes/batch", json={
            "kind": "chapter",
            "sections": [{"title": f"第 {i} 章", "content": f"这一章讲雨在窗外下了整夜，编号 {i}。"}
                         for i in range(12)]})
        assert r.json()["created"] == 12

        idx = client.post(f"/studio/works/{wid}/index", json={})
        assert idx.status_code == 200 and idx.json()["nodes"] >= 1

        hit = client.post(f"/studio/works/{wid}/search", json={"q": "雨在窗外下", "scope": "body"})
        assert hit.status_code == 200
        assert hit.json()["count"] >= 1

        stats = client.get(f"/studio/works/{wid}/stats").json()
        assert stats["nodes"] == 12
