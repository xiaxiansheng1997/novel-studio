"""去 AI 味改写（deai）：定位、贴回、提示词、画像兜底。

不测模型本身：真正决定成败的是「命中清单 → 要改哪几段」与「模型回复 →
贴回原文」这两段纯函数，模型只负责中间的改写。这两处错一个，改完的稿子
看起来成功、实际可能只动了一段，或者整节的段落编号全部错位。
"""
from __future__ import annotations

import pytest

from app.studio import aivoice, assembler, prompting, tasks
from app.studio.proofread import _paragraphs
from app.studio.store import StudioStore

LINE = "他推开门，屋里没人说话。桌上的茶凉了，窗外有雨声。"


# ==================== 回复解析 ====================

def test_parse_deai_reads_pieces() -> None:
    text = "【3】\n他放下杯子。\n\n【7】\n门外有人。两步一停。"
    assert prompting.parse_deai(text) == {3: "他放下杯子。", 7: "门外有人。两步一停。"}


def test_parse_deai_ignores_chatter() -> None:
    """模型爱在开头加一句「好的，改写如下」：不该被当成正文贴回去。"""
    text = "好的，改写如下：\n\n【3】\n他放下杯子。"
    assert prompting.parse_deai(text) == {3: "他放下杯子。"}


# ==================== 定位：命中清单 → 要改哪几段 ====================

def test_paragraphs_with_finds_every_hit() -> None:
    """命中项只带一处证据，但同一段里出现两次也得算这一处。"""
    content = "他缓缓起身。\n\n中间。\n\n他轻轻推门。"
    assert aivoice.paragraphs_with(content, "filler_adverb") == [1, 3]


def test_targets_follow_hits() -> None:
    content = ("他心里五味杂陈，仿佛有什么东西碎了。"
               "\n\n他在码头等了很久。"
               "\n\n他缓缓起身，轻轻推门。")
    hits = [{"rule": "emotion_label", "location": "1"},
            {"rule": "filler_adverb", "location": "3"}]
    got = tasks._deai_targets(content, hits)
    assert [idx for idx, _ in got] == [1, 3]


def test_targets_capped() -> None:
    """命中段太多时只改最集中的前几段：一次改十段，模型写到后面会走神。"""
    content = "\n\n".join([f"第 {i} 段——他说完就走了。" for i in range(1, 11)])
    got = tasks._deai_targets(content, [{"rule": "dash_explain"}])
    assert len(got) == tasks.DEAI_MAX_PARAS


# ==================== 贴回：模型回复 → 新正文 ====================

def _apply(content: str, rewrites: dict[int, str]):
    return tasks._deai_apply(content, rewrites, dict(_paragraphs(content)))


def test_apply_replaces_in_place() -> None:
    content = "第一段。\n\n第二段。\n\n第三段。"
    out, done, rejected, _same = _apply(content, {2: "改过的第二段。"})
    assert out == "第一段。\n\n改过的第二段。\n\n第三段。"
    assert done == [2] and rejected == []


def test_apply_rejects_split() -> None:
    """模型把一段拆成两段：贴回去会让后面所有段号错位，必须拒。"""
    content = "第一段。\n\n第二段。"
    out, done, rejected, _same = _apply(content, {1: "第一段。\n\n新加的一段。"})
    assert out == content
    assert done == [] and rejected == [1]


def test_apply_rejects_length_runaway() -> None:
    """改写后长度翻五倍：那不是改句式，是重写了。"""
    content = "第一段。\n\n第二段。"
    out, done, rejected, _same = _apply(content, {1: "很" * 100})
    assert done == [] and rejected == [1]


def test_apply_allows_expanding_a_short_paragraph() -> None:
    """短段落不比比例：一句「她转身。」扩成一句描写是合法改写。

    纯比例卡死时，5 字扩到 13 字（2.6 倍）会被拒，而短段恰恰是最该改的
    ——AI 腔的一大特征就是「她缓缓转身，眼中闪过一丝复杂」。
    """
    content = "她转身。\n\n第二段文字足够长，不会被当成短段落。"
    out, done, rejected, _same = _apply(content, {1: "她把手里的杯子放在桌沿上。"})
    assert done == [1] and rejected == []
    assert out.startswith("她把手里的杯子放在桌沿上。")


def test_apply_handles_repeated_paragraphs() -> None:
    """同一段文字出现两次：两处都要改，且各改各的。"""
    content = LINE + "\n\n中间一段。\n\n" + LINE
    first = "他推开门，屋里没人说话。茶凉了，窗外有雨。"
    second = "他推开门，屋里没人应声。茶凉了，窗外还在下雨。"
    out, done, _rejected, _same = _apply(content, {1: first, 3: second})
    assert out == first + "\n\n中间一段。\n\n" + second
    assert done == [1, 3]


def test_apply_does_not_touch_earlier_duplicate() -> None:
    """只提交后一处时，前面一模一样的段必须原样保留。

    这是 `content.find(old)` 的失效场景：它命中的是第 1 段，改完还报
    「已改 1 段」。而 `_deai_targets` 只挑权重最高的 6 段，「只选中后一处」
    是常态而不是边角情况——用户没让改的段被静默覆盖写进库里。
    """
    content = LINE + "\n\n中间一段文字。\n\n" + LINE
    second = "他推开门，屋里没人应声。茶凉了，窗外还在下雨。"
    out, done, rejected, _same = _apply(content, {3: second})
    assert out == LINE + "\n\n中间一段文字。\n\n" + second
    assert done == [3] and rejected == []


def test_apply_reports_copied_paragraphs_separately() -> None:
    """模型原样抄回不算「被拒」：两者混在一起的报错会指错方向。"""
    content = "第一段。\n\n第二段。"
    out, done, rejected, same = _apply(content, {1: "第一段。"})
    assert out == content
    assert done == [] and rejected == [] and same == [1]


# ==================== 提示词 ====================

def test_deai_prompt_lists_problems_by_paragraph() -> None:
    _system, user = prompting.deai_prompt(
        [(3, "他缓缓起身。")],
        [{"rule": "filler_adverb", "location": "3", "message": "副词过多"}])
    assert "【3】" in user and "他缓缓起身。" in user
    assert "第 3 段：副词过多" in user


def test_deai_prompt_carries_requirements() -> None:
    """改写也要带文体要求：不带的话模型会把小说段改成通稿腔，AI 味换一种而已。"""
    system, _user = prompting.deai_prompt([], [], "第三人称叙事。")
    assert "第三人称叙事。" in system


# ==================== 文风画像兜底 ====================

@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def _two_sections(store: StudioStore, content: str = "") -> tuple[dict, dict]:
    work = await store.create_work(kind="novel", title="画像兜底", scale="single")
    await store.create_nodes_batch(work_id=work["id"], kind="chapter", sections=[
        {"title": "第 1 章", "content": "\n\n".join([LINE] * 20)},
        {"title": "第 2 章", "content": content},
    ])
    tree = await store.list_tree(work["id"])
    return work, await store.get_node(tree[1]["id"])


async def test_voice_falls_back_to_written_sections(store: StudioStore) -> None:
    """没贴过旧文时，用本书已写章节现算一版，并标明这是基线。"""
    work, node = await _two_sections(store)
    res = await assembler.assemble(store, work, node, "draft")
    assert "文风基线" in res.system
    assert "不是模仿目标" in res.system


async def test_own_voice_profile_is_not_labelled_baseline(store: StudioStore) -> None:
    """贴过旧文时是「照这个写」：和兜底的基线是两种相反的语义。"""
    await store.set_setting("voice_profile", "平均句长 18 字。")
    work, node = await _two_sections(store)
    res = await assembler.assemble(store, work, node, "draft")
    assert "按作者自己的文风写" in res.system
    assert "文风基线" not in res.system


async def test_voice_fallback_skips_current_section(store: StudioStore) -> None:
    """正在写的这一节是半成品：混进样本会把句长统计拉歪。"""
    work, node = await _two_sections(store, content="\n\n".join([LINE] * 20))
    res = await assembler.assemble(store, work, node, "draft")
    # 只有第 1 章可当样本；把第 2 章也算进来就成了 2 节
    assert "1 节正文" in res.system
    assert "2 节正文" not in res.system
    # 画像首行也得说「节正文」，不能说成「篇旧文」——那会读成外部范文
    assert "篇旧文" not in res.system


async def test_voice_fallback_prefers_recent_sections(store: StudioStore) -> None:
    """取本节之前最近的几节，不是开篇几节。

    文案承诺的是「保持前后一致」：写到第 7 章时拿第 1-5 章的句长当基线，
    恰好是「和开篇一致」，与承诺相反。
    """
    work = await store.create_work(kind="novel", title="取最近的节", scale="single")
    # 第 1 章 300 字，第 2-6 章各 500 字：取最近 5 节是 2500 字，取最前是 2300
    await store.create_nodes_batch(work_id=work["id"], kind="chapter", sections=[
        {"title": "第 1 章", "content": "\n\n".join([LINE] * 12)},
        *({"title": f"第 {i} 章", "content": "\n\n".join([LINE] * 20)}
          for i in range(2, 7)),
        {"title": "第 7 章", "content": ""},
    ])
    tree = await store.list_tree(work["id"])
    node = await store.get_node(tree[6]["id"])
    text = await assembler._voice_from_book(store, work, node)
    assert "5 节正文" in text
    assert "2500 字" in text
