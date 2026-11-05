"""整本重建索引的提交边界（导入完那一步）。

为什么单测这个：2000 章逐单元提交，光 Windows 上的刷盘就 14 秒（实测一次
commit ≈ 7ms），而重建索引是派生数据的整批重算，本可以几个一批。改成按批
提交之后要同时钉住两件事：
  1) 批提交的数据真的落盘了——不是攒在连接里等下一次谁顺手 commit；
  2) 中途抛错只丢当前这一批，已提交的批留着、连接里不留尾巴；
     否则下一次无关的提交会把半截索引悄悄带进库。
第 3 条是给其它调用方（写作台单节补索引、任务流水线）上的保险：默认
`index_node` 必须自己提交，不能因为批量路径改了默认值。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.studio import indexer
from app.studio.indexer import reindex_work
from app.studio.store import StudioStore


def raw_count(db_path, sql: str) -> int:
    """另开一条连接数数：只有真提交过的东西它才看得见。"""
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def seed(store: StudioStore, n: int = 120) -> str:
    work = await store.create_work(kind="novel", title="索引边界", scale="series")
    wid = str(work["id"])
    for i in range(0, n, 200):
        await store.create_nodes_batch(work_id=wid, kind="chapter", sections=[
            {"title": f"第 {j + 1} 章", "content": f"雨在窗外下了整夜，编号 {j + 1}。"}
            for j in range(i, min(i + 200, n))])
    return wid


async def test_reindex_commits_in_batches_and_is_durable(store, tmp_path, monkeypatch) -> None:
    wid = await seed(store, 120)
    commits: list[int] = []
    real = store.commit

    async def counting() -> None:
        commits.append(1)
        await real()

    monkeypatch.setattr(store, "commit", counting)
    res = await reindex_work(store, wid)

    assert res["nodes"] == 120 and res["skipped"] == 0
    # 50 + 50 + 最后一批 20：三次提交，不是 120 次
    assert len(commits) == 3
    db = tmp_path / "studio.db"
    assert raw_count(db, "SELECT COUNT(*) FROM chunks") == res["node_chunks"]
    assert raw_count(db, f"SELECT COUNT(*) FROM index_units WHERE work_id = '{wid}'") == 120


async def test_failure_drops_only_the_current_batch(store, tmp_path, monkeypatch) -> None:
    """第 60 章炸掉：前 50 章已提交并可见，第 51~60 章不能留在连接里。"""
    wid = await seed(store, 120)
    db = tmp_path / "studio.db"
    real = indexer.index_node
    state = {"n": 0}

    async def boom(st, node, **kwargs):
        state["n"] += 1
        if state["n"] == 60:
            raise RuntimeError("分到一半炸了")
        return await real(st, node, **kwargs)

    monkeypatch.setattr(indexer, "index_node", boom)
    with pytest.raises(RuntimeError):
        await reindex_work(store, wid)
    assert state["n"] == 60

    assert raw_count(db, "SELECT COUNT(*) FROM index_units") == 50
    # 失败后连接必须是干净的：再提交一次也不能把 51~60 章带进库
    await store.commit()
    assert raw_count(db, "SELECT COUNT(*) FROM index_units") == 50
    assert raw_count(db, "SELECT COUNT(*) FROM chunks") == 50

    # 修好再跑：已提交的 50 章跳过，剩下的补齐
    monkeypatch.setattr(indexer, "index_node", real)
    res = await reindex_work(store, wid)
    assert res["skipped"] == 50 and res["nodes"] == 70
    assert raw_count(db, "SELECT COUNT(*) FROM index_units") == 120


async def test_single_node_still_commits_by_default(store, tmp_path) -> None:
    """写作台/任务流水线是单节调用：默认路径必须当场落盘，不能等别人来提交。"""
    wid = await seed(store, 1)
    node = (await store.list_tree(wid))[0]
    full = await store.get_node(node["id"])
    n = await indexer.index_node(store, full)
    assert n > 0
    assert raw_count(tmp_path / "studio.db", "SELECT COUNT(*) FROM chunks") == n
