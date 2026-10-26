"""删东西：`works` / `nodes` / `sources` 上的级联外键管不到的那几张表，得自己清干净。

发现过程很偶然——批量清完书架后跑了一遍孤儿行统计，`runs` 里 54 条、`index_units` 里
58 条、`chunks_fts` 里 63 条都指向早已不存在的作品。原因有两类：这几张表建得早，
`work_id` 上没写 `REFERENCES`；而 FTS5 是虚拟表，外键根本管不到它。

同一类坑在 `delete_node` / `delete_source` 上更隐蔽：作品还在，行却指向已删的节点，
所以按 work_id 查根本查不出来。其中 `summaries` 不只是虚胖——S4「最近节摘要」按
work_id 全量取，删掉的章节会以摘要的形式继续出现在每次生成的上下文里。
"""
from __future__ import annotations

import pytest

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


async def seed_work(store, title: str) -> dict:
    """建一本「每张表都写过一笔」的作品：正文块、索引台账、任务、生成记录。"""
    work = await store.create_work(kind="novel", title=title)
    node = await store.create_node(work_id=work["id"], kind="chapter", title="第一章",
                                   content="夜色沉下来，灯还亮着。" * 30, ordinal=1)
    assert await indexer.index_node(store, node) > 0
    await store.enqueue_job(work_id=work["id"], node_id=node["id"], task_type="generate")
    await store.start_run(work_id=work["id"], node_id=node["id"], task_type="generate",
                          model="m", provider="p", prompt_chars=10)
    return work


async def test_delete_work_clears_tables_without_foreign_keys(store) -> None:
    work = await seed_work(store, "会消失的书")
    assert await store.list_jobs(work_id=work["id"])
    assert await store.list_runs(work["id"])
    assert await store.list_index_units(work["id"])
    assert await store.count_chunks(work["id"])
    assert await store.orphan_counts() == {}  # 还没删，此时不该有孤儿

    assert await store.delete_work(work["id"]) is True

    assert await store.list_jobs(work_id=work["id"]) == []
    assert await store.list_runs(work["id"]) == []
    assert await store.list_index_units(work["id"]) == []
    assert await store.count_chunks(work["id"]) == 0
    # chunks 靠级联删掉了，chunks_fts 里的行只能由 delete_work 清：漏一条就是孤儿
    assert await store.orphan_counts() == {}


async def test_delete_one_work_leaves_the_other_intact(store) -> None:
    gone = await seed_work(store, "删掉的")
    kept = await seed_work(store, "留下的")

    await store.delete_work(gone["id"])

    assert len(await store.list_runs(kept["id"])) == 1
    assert len(await store.list_jobs(work_id=kept["id"])) == 1
    assert await store.count_chunks(kept["id"]) > 0
    assert await store.list_index_units(kept["id"])
    assert await store.orphan_counts() == {}


async def test_delete_unknown_work_returns_false(store) -> None:
    assert await store.delete_work("w_not_here") is False


async def test_delete_node_clears_fts_ledger_and_summary(store) -> None:
    """删一节：`chunks` 靠级联，`chunks_fts` / `index_units` / `summaries` 得 delete_node 自己清。"""
    work = await store.create_work(kind="novel", title="三章书")
    nodes = []
    for i in (1, 2, 3):
        node = await store.create_node(work_id=work["id"], kind="chapter", title=f"第{i}章",
                                       content=f"第{i}章的正文。" * 40, ordinal=i)
        assert await indexer.index_node(store, node) > 0
        await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                                   content=f"【{i}】事件/变化/钩子", ref_ordinal=i)
        nodes.append(node)
    chunks_before = await store.count_chunks(work["id"])

    assert await store.delete_node(nodes[1]["id"]) is True

    # 剩下两节的块还在，被删那一节的已经跟着节点一起没了
    chunks_after = await store.count_chunks(work["id"])
    assert 0 < chunks_after < chunks_before
    assert len(await store.list_index_units(work["id"])) == 2
    kept = {s["ref_id"] for s in await store.list_summaries(work["id"], "node")}
    assert kept == {nodes[0]["id"], nodes[2]["id"]}
    # 关键一条：倒排索引里不能留下指向已被级联删掉的块的行
    assert await store.orphan_counts() == {}


async def test_delete_volume_clears_whole_subtree(store) -> None:
    """删卷要连子树一起清干净，包括卷自己的 container 摘要。"""
    work = await store.create_work(kind="novel", title="带卷的书", scale="series")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    chapter = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                      title="第一章", content="雪落了满山。" * 40, ordinal=1)
    assert await indexer.index_node(store, chapter) > 0
    await store.upsert_summary(work["id"], scope="container", ref_id=vol["id"], content="本卷概要")

    assert await store.delete_node(vol["id"]) is True

    assert await store.count_chunks(work["id"]) == 0
    assert await store.list_index_units(work["id"]) == []
    assert await store.list_summaries(work["id"], "container") == []
    assert await store.orphan_counts() == {}


async def test_delete_source_clears_fts_and_ledger(store) -> None:
    """素材块同样会被级联带走，倒排索引与台账得手动清。"""
    work = await store.create_work(kind="article", title="带素材的书")
    src = await store.add_source(work["id"], content="某份素材。" * 40, title="素材一")
    assert await indexer.index_source(store, await store.get_source(src["id"])) > 0
    assert await store.count_chunks(work["id"], "source") > 0

    assert await store.delete_source(src["id"]) is True

    assert await store.count_chunks(work["id"], "source") == 0
    assert await store.list_index_units(work["id"]) == []
    assert await store.orphan_counts() == {}


async def test_sweep_orphans_removes_node_level_leftovers(store) -> None:
    """`delete_node` 修好之前删掉的章节，留下的台账行与摘要行要有入口清。

    这些行按 work_id 查不出来（作品还活着），所以下面先断言它们只以「指向不存在的节点」
    的形式被认出来。
    """
    live = await seed_work(store, "活着的书")
    await store.set_index_stamp(ref_kind="node", ref_id="n_已删的节", work_id=live["id"],
                                content_hash="deadbeef")
    await store.upsert_summary(live["id"], scope="node", ref_id="n_已删的节",
                               content="旧章节摘要", ref_ordinal=99)

    before = await store.orphan_counts()
    assert before == {"index_units": 1, "summaries": 1}
    assert await store.sweep_orphans() == before
    assert await store.orphan_counts() == {}
    # 活作品的台账、正文块、摘要一条都不能少
    assert await store.list_index_units(live["id"])
    assert await store.count_chunks(live["id"]) > 0
    assert len(await store.list_summaries(live["id"], "node")) == 0
    assert len(await store.list_runs(live["id"])) == 1


async def test_sweep_orphans_removes_legacy_leftovers(store) -> None:
    """`delete_work` 修好之前删掉的作品，留下的行要有入口清。"""
    live = await seed_work(store, "还在的书")
    ghost = "w_已经不存在了"
    await store.enqueue_job(work_id=ghost, node_id=None, task_type="generate")
    await store.start_run(work_id=ghost, node_id=None, task_type="generate",
                          model="m", provider="p", prompt_chars=10)
    await store.set_index_stamp(ref_kind="node", ref_id="n_ghost", work_id=ghost,
                                content_hash="deadbeef")
    # FTS 虚拟表不吃外键，直接塞一行模拟「块没了、索引行还在」
    async with store._lock:
        await store._db.execute(
            "INSERT INTO chunks_fts(chunk_id, work_id, text_tok) VALUES(?,?,?)",
            ("c_ghost", ghost, "ghost"))
        await store._db.commit()

    before = await store.orphan_counts()
    assert before == {"jobs": 1, "runs": 1, "index_units": 1, "chunks_fts": 1}

    assert await store.sweep_orphans() == before
    assert await store.orphan_counts() == {}
    # 只清孤儿：活作品的行一条都不能少
    assert len(await store.list_runs(live["id"])) == 1
    assert len(await store.list_jobs(work_id=live["id"])) == 1
    assert await store.count_chunks(live["id"]) > 0
    assert await store.list_index_units(live["id"])
