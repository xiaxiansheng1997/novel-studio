"""改完正文，摘要必须过期——否则写作台会拿旧记忆去写下一节。

这里的坑原本是这么藏的：`summarize_node` 判断「要不要重算」比的是 `ref_ordinal == node.ordinal`，
也就是**序号**。序号在改稿时根本不变，于是重写第 7 章之后摘要还是上一版，而 S4「最近节摘要」
会拿着这份旧记忆去写第 8 章——已经改掉的情节被当成既定事实喂给模型。修法是给 `summaries`
加内容指纹 `ref_hash`，口径与 `nodes.content_hash` 完全一致（未 strip 的原文，sha1 前 16 位）。

指纹一上，两个新问题立刻跟着来，所以本文件的用例分三类：
1. **该重算的要重算**（正文改了 → 节摘要过期 → 上游卷/全书摘要跟着置空 → 过期的一律不注入）；
2. **不该重算的别重算**（正文没改、只改标题、只加尾部空格 → 一次调用都不许多烧）；
3. **老库升级当天不能翻脸**（`_migrate` 必须回填历史指纹，否则现存摘要全被判过期，
   看起来就像「记忆全丢了」）。

全程假 gateway，只记账不打网络：调用次数本身就是断言对象。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.studio import assembler, indexer, summarizer, tasks
from app.studio.store import StudioStore, _hash

BODY = "夜色沉下来，灯还亮着。"  # 11 字，重复后过 summarize_node 的 200 字门槛


class FakeGateway:
    """只记录被调了几次，顺便让每次返回的文本都不一样，好在断言里认出是哪一版。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        self.calls.append(prompt)
        return SimpleNamespace(text=f"记忆卡 #{len(self.calls)}")

    @property
    def calls_n(self) -> int:
        return len(self.calls)


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def make_chapters(store, n: int = 2, *, volume: bool = False) -> tuple[dict, dict, list[dict]]:
    """造一本带 n 章的书；`volume=True` 时挂在同一卷下（用来验上游摘要的失效传播）。"""
    work = await store.create_work(kind="novel", title="记忆测试", scale="series" if volume else "single")
    vol = None
    if volume:
        vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    chapters = []
    for i in range(1, n + 1):
        node = await store.create_node(
            work_id=work["id"], kind="chapter", title=f"第{i}章",
            content=(f"第{i}章：" + BODY) * 30, ordinal=i,
            **({"parent_id": vol["id"]} if vol else {}),
        )
        chapters.append(node)
    return work, vol, chapters


# ==================== 该重算的必须重算 ====================

async def test_edited_body_forces_recompute(store) -> None:
    """正文改了 → 第二次 `summarize_node` 必须真的再调一次模型，并把摘要换掉。

    这就是原来那个 bug：改稿之后调用被当成「已经有摘要了」跳过。
    """
    gw = FakeGateway()
    work, _, chapters = await make_chapters(store, 1)
    ch = chapters[0]

    first = await summarizer.summarize_node(gw, store, ch, model="m")
    assert gw.calls_n == 1 and first == "记忆卡 #1"

    await store.update_node(ch["id"], content=("改成别的剧情了，" + BODY) * 30)
    second = await summarizer.summarize_node(gw, store, await store.get_node(ch["id"]), model="m")

    assert gw.calls_n == 2, "改了正文却没重算，摘要就会拿旧情节喂下一节"
    assert second == "记忆卡 #2"
    row = await store.get_summary("node", ch["id"])
    assert row["content"] == "记忆卡 #2" and row["fresh"]
    assert await store.stale_summaries(work["id"]) == []


async def test_upstream_summaries_goes_stale_with_the_chapter(store) -> None:
    """卷摘要与全书摘要是多节拼出来的，没有哪个字段能反映它旧了——只能靠失效传播置空。"""
    gw = FakeGateway()
    work, vol, chapters = await make_chapters(store, 2, volume=True)
    done = await summarizer.summarize_work_batch(gw, store, work["id"], model="m", scope="all")
    assert done == {"nodes": 2, "containers": 1, "work": 1}
    assert gw.calls_n == 4

    await store.update_node(chapters[1]["id"], content=("第2章整个重写。" + BODY) * 30)

    assert (await store.get_summary("container", vol["id"]))["ref_hash"] == ""
    assert (await store.get_summary("work", work["id"]))["ref_hash"] == ""
    stale = await store.stale_summaries(work["id"])
    assert {s["scope"] for s in stale} == {"node", "container", "work"}
    # 节摘要自己不需要显式标：ref_hash 与新 content_hash 自然对不上
    assert (await store.get_summary("node", chapters[0]["id"]))["fresh"]


async def test_stale_summaries_are_never_injected(store) -> None:
    """装配阶段是最后一道闸：过期摘要不进上下文，但要留一句人能看见的提示。"""
    gw = FakeGateway()
    work, vol, chapters = await make_chapters(store, 3, volume=True)
    await summarizer.summarize_work_batch(gw, store, work["id"], model="m", scope="all")

    await store.update_node(chapters[1]["id"], content=("第2章改过了。" + BODY) * 30)
    res = await assembler.assemble(store, work, await store.get_node(chapters[2]["id"]),
                                   "draft", "接着写第3章")

    assert any("摘要已过期" in w for w in res.warnings), res.warnings
    assert "第 2 节《" not in res.user, "过期节摘要不能占 S4 的名额"
    assert "本卷/本讲概要" not in res.user, "S3 的卷摘要已过期，整段都不该注入"
    assert "第 1 节《" in res.user, "没改过的那些节必须照常注入"


async def test_prev_summary_skips_stale_row(store) -> None:
    """章纲（beats）用的「上一节发生了什么」同样得过这一道闸。"""
    gw = FakeGateway()
    work, _, chapters = await make_chapters(store, 1)
    await summarizer.summarize_node(gw, store, chapters[0], model="m")
    assert await tasks._prev_summary(store, work["id"], 2) == "记忆卡 #1"

    await store.update_node(chapters[0]["id"], content=("上一节改了剧情。" + BODY) * 30)
    assert await tasks._prev_summary(store, work["id"], 2) == ""


# ==================== 不该重算的一节都不许多烧 ====================

async def test_unchanged_body_is_skipped(store) -> None:
    """正文没动还是每次都重算，等于把摘要的成本翻着跟头往上加。"""
    gw = FakeGateway()
    _, _, chapters = await make_chapters(store, 1)
    await summarizer.summarize_node(gw, store, chapters[0], model="m")

    again = await summarizer.summarize_node(gw, store, await store.get_node(chapters[0]["id"]),
                                            model="m")
    assert gw.calls_n == 1, "内容未变却重跑了一次摘要"
    assert again == "记忆卡 #1"


async def test_fingerprint_uses_the_unstripped_body(store) -> None:
    """口径护栏：首尾空白也是内容的一部分。

    摘要这边若先 strip 再算，而 `nodes.content_hash` 按原文算，两边永远对不上，
    每一节都会被判过期 —— 于是每次生成都在重跑全部摘要。
    """
    raw = "\n  " + ("带空白的正文。" + BODY) * 30 + "\n\n  "
    work = await store.create_work(kind="novel", title="空白口径")
    node = await store.create_node(work_id=work["id"], kind="chapter", title="第一章",
                                   content=raw, ordinal=1)
    gw = FakeGateway()
    await summarizer.summarize_node(gw, store, node, model="m")

    row = await store.get_summary("node", node["id"])
    assert row["ref_hash"] == _hash(raw) == (await store.get_node(node["id"]))["content_hash"]
    assert row["fresh"] and await store.stale_summaries(work["id"]) == []

    # 自动保存会把同一个正文重复写回：内容字节没变就不该判过期
    await store.update_node(node["id"], content=raw)
    assert await summarizer.summarize_node(gw, store, await store.get_node(node["id"]),
                                          model="m") == "记忆卡 #1"
    assert gw.calls_n == 1
    assert (await store.get_summary("node", node["id"]))["fresh"]


async def test_title_only_edit_keeps_everything_fresh(store) -> None:
    """改标题不是改剧情，不能把卷摘要与全书摘要一起拖去重算。"""
    gw = FakeGateway()
    work, vol, chapters = await make_chapters(store, 2, volume=True)
    await summarizer.summarize_work_batch(gw, store, work["id"], model="m", scope="all")

    await store.update_node(chapters[0]["id"], title="换了个章名")

    assert (await store.get_summary("container", vol["id"]))["ref_hash"] != ""
    assert await store.stale_summaries(work["id"]) == []


# ==================== 老库升级 ====================

async def test_migration_backfills_history(tmp_path) -> None:
    """已有库必须走 ALTER 补列，并把历史行的指纹回填上。

    回填这一步要紧：`ref_hash` 默认空串，而空串在我们的口径里就是「已过期」。
    不回填的话升级当天现存摘要全被判过期，S3/S4 直接空掉，看起来像记忆全丢。
    """
    db = tmp_path / "studio.db"
    s = StudioStore(db)
    await s.init()
    work = await s.create_work(kind="novel", title="升级前的库")
    node = await s.create_node(work_id=work["id"], kind="chapter", title="第一章",
                               content=("旧稿正文。" + BODY) * 30, ordinal=1)
    # 退回升级前的样子：这一列压根不存在，历史摘要只剩文本
    async with s._lock:
        await s._db.execute("ALTER TABLE summaries DROP COLUMN ref_hash")
        for sid, scope, ref in (("sm_old", "node", node["id"]), ("sm_work", "work", work["id"])):
            await s._db.execute(
                "INSERT INTO summaries(id, work_id, scope, ref_id, ref_ordinal, content, "
                "model, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (sid, work["id"], scope, ref, 1 if scope == "node" else 0,
                 f"升级前的{scope}摘要", "m", 1.0))
        await s._db.commit()
    await s.close()

    s2 = StudioStore(db)
    await s2.init()
    try:
        rows = {r["id"]: r for r in await s2.list_summaries(work["id"])}
        assert rows["sm_old"]["ref_hash"] == (await s2.get_node(node["id"]))["content_hash"]
        assert rows["sm_work"]["ref_hash"] == "legacy"
        assert await s2.stale_summaries(work["id"]) == []
        # 回填没做到的话，这里就会白烧一次调用（重开库的人根本看不出区别，只有账本记得）
        gw = FakeGateway()
        assert await summarizer.summarize_node(gw, s2, await s2.get_node(node["id"]),
                                               model="m") == "升级前的node摘要"
        assert gw.calls_n == 0
    finally:
        await s2.close()


async def test_index_status_counts_stale_rows(store) -> None:
    """右栏那个「N 份已过期」与「重算过期摘要」按钮全靠这两个计数。"""
    gw = FakeGateway()
    work, _, chapters = await make_chapters(store, 2, volume=True)
    await summarizer.summarize_work_batch(gw, store, work["id"], model="m", scope="all")

    st = await indexer.index_status(store, work["id"])
    assert (st["stale_summaries"], st["stale_upstream"]) == (0, 0)

    await store.update_node(chapters[1]["id"], content=("改了一章。" + BODY) * 30)
    st = await indexer.index_status(store, work["id"])
    assert (st["stale_summaries"], st["stale_upstream"]) == (1, 2)
