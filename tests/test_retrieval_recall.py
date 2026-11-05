"""检索召回：摘要通道 + 双路 RRF + 乘性近因 + 短语优先。

这四件事坏掉都不会当场报错——生成照样跑完，只是模型手上的证据越来越偏：
要么全是最近几节（近因是绝对加项时，2000 章下 top8 全落在当前节附近），
要么一条几十章前埋的伏笔都想不起来（正文块按 BM25 排进不了前几名，
而那条线索其实好端端地躺在摘要里）。

所以分两层测：
- **落库层**用真 SQLite：摘要倒排是否跟着 upsert 换、过期与「最近 N 节」是否被挡住、
  老库能不能靠 `rebuild_summaries_fts` 补票；
- **排序层**用假 store：分数怎么算、谁排谁前面，用可控的 bm25 与章号直接钉死。
"""
from __future__ import annotations

import pytest

from app.studio import indexer, retrieval, tokenize
from app.studio.store import StudioStore

# 专名先灌进词典，否则 jieba 会把它们切碎，用例断言的就不再是「倒排对不对」
tokenize.add_words(["玄铁令", "断魂崖", "寒潭"])


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def make_node(store, work_id: str, *, ordinal: int, content: str) -> dict:
    return await store.create_node(
        work_id=work_id, kind="chapter", title=f"第{ordinal}章", content=content, ordinal=ordinal)


def digest_of(node: dict) -> str:
    """摘要要带上正文指纹才算「没过期」，否则 `search_summaries` 一律不出。"""
    return node["content_hash"]


# ==================== 落库层：倒排跟着摘要走 ====================

async def test_summary_fts_follows_upsert(store) -> None:
    """摘要改写后旧词必须搜不到：倒排不换，S5 拿回来的就是上一版记忆。"""
    work = await store.create_work(kind="novel", title="摘要倒排")
    node = await make_node(store, work["id"], ordinal=1, content="夜色沉下来，灯还亮着。" * 30)
    node = await store.get_node(node["id"])

    await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                               content="玄铁令落在了断魂崖。", ref_ordinal=1,
                               ref_hash=digest_of(node))
    hits = await store.search_summaries(work["id"], tokenize.match_query("玄铁令 断魂崖"))
    assert [h["ref_id"] for h in hits] == [node["id"]]

    await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                               content="玄铁令被沉进了寒潭。", ref_ordinal=1,
                               ref_hash=digest_of(node))
    assert await store.search_summaries(work["id"], tokenize.match_query("断魂崖")) == []
    hits = await store.search_summaries(work["id"], tokenize.match_query("寒潭"))
    assert [h["ref_id"] for h in hits] == [node["id"]]


async def test_stale_summary_stays_out_of_recall(store) -> None:
    """正文一改，那份摘要就不再是「这本书里发生过什么」，不能再当证据。"""
    work = await store.create_work(kind="novel", title="过期摘要")
    node = await make_node(store, work["id"], ordinal=1, content="玄铁令落在了断魂崖。" * 20)
    node = await store.get_node(node["id"])
    await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                               content="玄铁令落在了断魂崖。", ref_ordinal=1,
                               ref_hash=digest_of(node))
    assert len(await store.search_summaries(work["id"], tokenize.match_query("玄铁令"))) == 1

    await store.update_node(node["id"], content="改成了别的剧情。" * 20)

    assert await store.search_summaries(work["id"], tokenize.match_query("玄铁令")) == []


async def test_recent_window_is_left_to_s4(store) -> None:
    """最近几节的摘要 S4 已经整块注入，检索里再回来一遍只是重复占预算。"""
    work = await store.create_work(kind="novel", title="近处留给 S4")
    for i in range(1, 11):
        node = await make_node(store, work["id"], ordinal=i, content=f"第{i}章正文。" * 20)
        node = await store.get_node(node["id"])
        await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                                   content=f"第{i}章玄铁令线索。", ref_ordinal=i,
                                   ref_hash=digest_of(node))

    hits = await store.search_summaries(
        work["id"], tokenize.match_query("玄铁令"),
        limit=20, max_ordinal=10 - retrieval.RECENT_WINDOW,
    )

    assert sorted(h["ref_ordinal"] for h in hits) == [1, 2, 3]


async def test_rebuild_backfills_an_old_library(store) -> None:
    """改造前建的库：摘要行都在，倒排一条也没有。重跑索引要把票补上，且幂等。"""
    work = await store.create_work(kind="novel", title="老库补票")
    node = await make_node(store, work["id"], ordinal=1, content="夜色沉下来。" * 20)
    node = await store.get_node(node["id"])
    await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                               content="玄铁令落在了断魂崖。", ref_ordinal=1,
                               ref_hash=digest_of(node))
    async with store._lock:
        await store._db.execute("DELETE FROM summaries_fts")
        await store._db.commit()
    assert await store.search_summaries(work["id"], tokenize.match_query("玄铁令")) == []

    assert await store.rebuild_summaries_fts(work["id"]) == 1
    assert await store.rebuild_summaries_fts(work["id"]) == 1
    assert len(await store.search_summaries(work["id"], tokenize.match_query("玄铁令"))) == 1
    assert await store.orphan_counts() == {}


async def test_migration_backfills_summaries_fts(tmp_path) -> None:
    """老库升级：表是新加的（空），历史摘要不补进去，摘要通道等于没开。"""
    db = tmp_path / "studio.db"
    s = StudioStore(db)
    await s.init()
    work = await s.create_work(kind="novel", title="升级前的库")
    node = await make_node(s, work["id"], ordinal=1, content="夜色沉下来。" * 20)
    node = await s.get_node(node["id"])
    await s.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                           content="玄铁令落在了断魂崖。", ref_ordinal=1,
                           ref_hash=digest_of(node))
    # 退回升级前的样子：摘要行都在，倒排一条也没有
    async with s._lock:
        await s._db.execute("DELETE FROM summaries_fts")
        await s._db.commit()
    await s.close()

    s2 = StudioStore(db)
    await s2.init()
    try:
        hits = await s2.search_summaries(work["id"], tokenize.match_query("玄铁令"))
        assert [h["ref_id"] for h in hits] == [node["id"]]
    finally:
        await s2.close()


async def test_recent_window_holds_at_its_boundary(store) -> None:
    """`current_ordinal == RECENT_WINDOW` 时窗口正好是 0。

    这里曾写成 `if max_ordinal:`——0 是假值，过滤整段失效，第 1..5 节
    在 S4 已整块注入之后又从 S5 回来一遍，重复占预算。
    """
    work = await store.create_work(kind="novel", title="窗口边界")
    for i in range(1, 7):
        node = await make_node(store, work["id"], ordinal=i, content=f"第{i}章正文。" * 20)
        node = await store.get_node(node["id"])
        await store.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                                   content=f"第{i}章玄铁令线索。", ref_ordinal=i,
                                   ref_hash=digest_of(node))
    match = tokenize.match_query("玄铁令")

    assert len(await store.search_summaries(work["id"], match, limit=20)) == 6
    # current_ordinal = 6 → max_ordinal = 0，六条全在窗口内，一条都不该出
    assert await store.search_summaries(
        work["id"], match, limit=20,
        max_ordinal=6 - retrieval.RECENT_WINDOW,
    ) == []


async def test_retrieval_drops_template_words_from_match() -> None:
    """检索路径要剔模板套话；跨作品搜索走同一个函数，那边保留（见 test_retrieval_query）。"""
    store = FakeStore([chunk("c1", ordinal=1, score=1.0)])

    await retrieval.retrieve(store, "w", query="本节 正文 篇幅 玄铁令 西市仓库")

    assert '"玄铁令"' in store.chunk_match and '"西市仓库"' in store.chunk_match
    for noise in ("本节", "正文", "篇幅"):
        assert f'"{noise}"' not in store.chunk_match, store.chunk_match


async def test_migration_feeds_term_glossary_before_tokenizing(tmp_path, monkeypatch) -> None:
    """回填时先灌术语表再切词。

    不灌的话写进去的是「青霄 / 剑宗」，而查询（`reindex_work` 灌过词典）切出来
    是「青霄剑宗」，专名永远命中不了——而专名恰恰是最重要的那类线索。
    只断言「调过 add_words」：切分结果依赖全局词典状态，在测试里无法可靠隔离。
    """
    db = tmp_path / "studio.db"
    s = StudioStore(db)
    await s.init()
    work = await s.create_work(kind="novel", title="带术语的旧库")
    node = await make_node(s, work["id"], ordinal=1, content="夜色沉下来。" * 20)
    node = await s.get_node(node["id"])
    await s.upsert_summary(work["id"], scope="node", ref_id=node["id"],
                           content="青霄剑宗在山门立誓。", ref_ordinal=1,
                           ref_hash=digest_of(node))
    await s.upsert_term(work["id"], term="青霄剑宗")
    async with s._lock:
        await s._db.execute("DELETE FROM summaries_fts")
        await s._db.commit()
    await s.close()

    batches: list[list[str]] = []
    real = tokenize.add_words
    monkeypatch.setattr(
        tokenize, "add_words",
        lambda words: (batches.append(list(words)), real(words))[1])

    s2 = StudioStore(db)
    await s2.init()
    try:
        assert any("青霄剑宗" in batch for batch in batches), \
            f"回填前没灌术语表，专名会被切碎: {batches}"
    finally:
        await s2.close()


async def test_summary_channel_reaches_the_old_foreshadow(store) -> None:
    """端到端：正文里一次都没写过那件东西，只有第一节的摘要记着，也要能召回。"""
    work = await store.create_work(kind="novel", title="远处的伏笔")
    for i in range(1, 9):
        node = await make_node(
            store, work["id"], ordinal=i,
            content=f"第{i}章：风雪压着山脊，队伍继续赶路。" * 12)
        assert await indexer.index_node(store, node) > 0
        node = await store.get_node(node["id"])
        await store.upsert_summary(
            work["id"], scope="node", ref_id=node["id"], ref_ordinal=i,
            content="玄铁令落在了断魂崖。" if i == 1 else f"第{i}章一路无事。",
            ref_hash=digest_of(node),
        )

    hits = await retrieval.retrieve(store, work["id"], query="玄铁令", current_ordinal=8)

    assert [h["ordinal"] for h in hits if h["source_kind"] == "summary"] == [1]


# ==================== 排序层：分数怎么算 ====================

def chunk(cid: str, ordinal: int, score: float, text: str = "") -> dict:
    return {"id": cid, "text": text or f"{cid} 的正文", "source_kind": "body",
            "node_id": f"n_{cid}", "ordinal": ordinal, "seq_in_unit": 1,
            "char_start": 0, "score": score}


def summary(sid: str, ordinal: int, score: float, text: str = "") -> dict:
    return {"id": sid, "ref_id": f"n_{sid}", "content": text or f"{sid} 的摘要",
            "ref_ordinal": ordinal, "score": score}


class FakeStore:
    """只喂两路召回的结果，不碰 SQLite：这一层要断言的是排序公式本身。"""

    def __init__(self, chunks: list[dict], summaries: list[dict] | None = None) -> None:
        self.chunks = chunks
        self.summaries = summaries or []
        self.summary_called: dict | None = None
        self.chunk_match = ""

    async def search_chunks(self, work_id, match, *, kinds=("body",), limit=20):
        self.chunk_match = match
        return list(self.chunks)

    async def search_summaries(self, work_id, match, *, limit=20, max_ordinal=None):
        self.summary_called = {"limit": limit, "max_ordinal": max_ordinal}
        return list(self.summaries)

    async def get_chunk_source_titles(self, work_id):
        return {}


async def test_recency_scales_instead_of_shifting() -> None:
    """同 base 的两块：近处最多高 15%，远处那块不会被「制造」出相关性。"""
    store = FakeStore([chunk("near", ordinal=1000, score=1.0),
                       chunk("far", ordinal=1, score=1.0)])

    hits = await retrieval.retrieve(store, "w", query="玄铁令", current_ordinal=1000)

    assert [h["chunk_id"] for h in hits] == ["near", "far"]
    assert hits[0]["score"] == pytest.approx(1 + retrieval.RECENCY_WEIGHT)
    assert hits[1]["score"] == pytest.approx(1.0)

    # 反过来：远处那条真命中，不能被近处一条弱相关越过去
    # （绝对加项模型下 0.25 的 base 加 0.35 就翻盘了）
    store = FakeStore([chunk("exact", ordinal=1, score=0.2),
                       chunk("junk", ordinal=1000, score=0.05)])
    hits = await retrieval.retrieve(store, "w", query="玄铁令", current_ordinal=1000)
    assert hits[0]["chunk_id"] == "exact"


async def test_phrase_hit_lifts_a_weaker_chunk() -> None:
    """地点/标题这种整串在原文里出现，比散词命中硬——命中就加一档。"""
    store = FakeStore([
        chunk("strong", ordinal=1, score=1.0, text="风雪压着山脊。"),
        chunk("plain", ordinal=1, score=0.5, text="队伍继续赶路。"),
        chunk("phrase", ordinal=1, score=0.5, text="他回到了断魂崖下。"),
    ])

    hits = await retrieval.retrieve(store, "w", query="玄铁令", phrases=["断魂崖"])

    assert [h["chunk_id"] for h in hits] == ["strong", "phrase", "plain"]
    by_id = {h["chunk_id"]: h for h in hits}
    assert by_id["phrase"]["score"] == pytest.approx(0.5 + retrieval.PHRASE_BONUS)
    assert by_id["plain"]["score"] == pytest.approx(0.5)


async def test_summary_channel_is_fused_by_rank() -> None:
    """两路各自的第一名在 RRF 里同分：摘要不是二等证据，也不抢原文的位置。"""
    store = FakeStore(
        chunks=[chunk(f"c{i}", ordinal=100 + i, score=1.0 - i * 0.1) for i in range(1, 4)],
        summaries=[summary("old", ordinal=1, score=1.0, text="玄铁令落在了断魂崖。")],
    )

    hits = await retrieval.retrieve(store, "w", query="玄铁令", current_ordinal=1000)

    assert store.summary_called["max_ordinal"] == 1000 - retrieval.RECENT_WINDOW
    kept = [h for h in hits if h["source_kind"] == "summary"]
    assert [h["ordinal"] for h in kept] == [1]
    assert kept[0]["score"] == pytest.approx(1.0)


async def test_summary_slots_keep_room_for_raw_text() -> None:
    """摘要是概述，最多占 3 条：再多就会把原文块挤出预算。"""
    store = FakeStore(
        chunks=[chunk("c1", ordinal=1, score=1.0)],
        summaries=[summary(f"s{i}", ordinal=i, score=1.0 - i * 0.01) for i in range(1, 6)],
    )

    hits = await retrieval.retrieve(store, "w", query="玄铁令", current_ordinal=1000)

    assert sum(1 for h in hits if h["source_kind"] == "summary") == retrieval.SUMMARY_SLOTS


async def test_sources_bucket_never_touches_summaries() -> None:
    """素材桶要的是原文，不是自己写的梗概：那条通道根本不该开。"""
    store = FakeStore([chunk("c1", ordinal=0, score=1.0)])

    await retrieval.retrieve(store, "w", query="玄铁令", kinds=("source",), current_ordinal=10)

    assert store.summary_called is None
