"""AI 味检测的九条规则（`app/studio/aivoice.py`）。

为什么单测：这套规则全是「频率阈值」判断，最容易犯两种错——

1. 阈值定太紧，正常小说全文报红，用户第一天就把告警关掉；
2. 阈值定太松，写三十章一条都没报，等于没做。

所以每条规则都配一组「该报」与「不该报」的对照，外加一段普通叙事文本
必须零告警的基线。基线刻意用最平的句子（推门、茶凉、雨声），不带任何
修辞：如果它都被报出问题，说明阈值本身错了，而不是文章有 AI 味。

最后两条走 `consistency.run_checks` 的接入层：AI 味只量小说，同一段正文
换成 article 不该再报——说明文与公文本来就该有总结句和排比。
"""
from __future__ import annotations

import pytest

from app.studio import aivoice, consistency
from app.studio.store import StudioStore

# 21 个汉字 × 16 段 = 336，刚过 MIN_CHARS=300 那道门槛
LINE = "他推开门，屋里没人说话。桌上的茶凉了，窗外有雨声。"
# 段末一律收在「他知道」上：这是 AI 最常见的升华句
TAIL_LINE = "他推开门，屋里的风灌进来，没人说话。他知道，这事完了。"
# 39 个汉字的一轮对白；四轮共 156 字，约能在千字里占到 15%
TALK_LINE = ("「你到底想怎么样。」「我想知道那晚你去了哪里。」"
             "「那晚我在码头。」「谁看见了。」「老周看见了，他就在船头。」")


def _body(n: int = 16) -> str:
    return "\n\n".join([LINE] * n)


def _rules(hits: list[dict]) -> set[str]:
    return {h["rule"] for h in hits}


def test_plain_prose_reports_nothing() -> None:
    """基线：没有修辞的叙事文本，七条规则一条都不该响。"""
    assert aivoice.check_node(_body()) == []


def test_short_text_is_skipped() -> None:
    """一节 200 字里出现两次「仿佛」会算成每千字 10 次，这种数没有意义。"""
    assert aivoice.check_node("他缓缓推开门，仿佛屋里有人。" * 3) == []


def test_short_text_boundary() -> None:
    """刚过 300 字就要开始评：门槛低了会漏，高了等于半节不检查。"""
    got = aivoice.check_node("\n\n".join([TAIL_LINE] * 15))
    assert "tail_summary" in _rules(got)


def test_dash_density_is_reported() -> None:
    text = _body() + "\n\n——他没说。——他也没问。"
    assert "dash_explain" in _rules(aivoice.check_node(text))


def test_single_dash_is_fine() -> None:
    """对话中断本来就要用破折号：一处不算问题，密了才算。"""
    text = _body() + "\n\n——他没说。"
    assert "dash_explain" not in _rules(aivoice.check_node(text))


def test_parallel_in_one_paragraph() -> None:
    """同段凑齐「不是…而是」+「既…又」：读者会听出这是机器在造句。"""
    text = _body(15) + "\n\n他不是不想走，而是走不了。他既怕死，又怕活着。"
    assert "parallel_triplet" in _rules(aivoice.check_node(text))


def test_single_parallel_is_fine() -> None:
    text = _body(15) + "\n\n他不是不想走，而是走不了。"
    assert "parallel_triplet" not in _rules(aivoice.check_node(text))


def test_tail_summary_ratio() -> None:
    assert "tail_summary" in _rules(aivoice.check_node("\n\n".join([TAIL_LINE] * 16)))


def test_paragraphs_ending_on_action_are_fine() -> None:
    """同样的段数与长度，只把末句换成动作：不该报——判的是「替读者下结论」。"""
    line = "他推开门，屋里的风灌进来，没人说话。茶凉了。"
    assert aivoice.check_node("\n\n".join([line] * 16)) == []


def test_emotion_labels() -> None:
    text = _body(15) + "\n\n他五味杂陈，难以言喻，心里涌上心头。"
    assert "emotion_label" in _rules(aivoice.check_node(text))


def test_hedge_stack() -> None:
    text = _body(15) + "\n\n仿佛有风，似乎有人，像是要走。"
    assert "hedge_stack" in _rules(aivoice.check_node(text))


def test_sensory_cliche() -> None:
    text = _body(15) + "\n\n空气中弥漫着酒味。时间仿佛静止。"
    assert "sensory_cliche" in _rules(aivoice.check_node(text))


def test_filler_adverb() -> None:
    text = _body(15) + "\n\n他缓缓起身，轻轻推门，静静站着，深深吸气。"
    assert "filler_adverb" in _rules(aivoice.check_node(text))


def test_transition_filler() -> None:
    text = _body(15) + "\n\n就在这时，门开了。与此同时，灯灭了。"
    assert "transition_filler" in _rules(aivoice.check_node(text))


def test_transition_in_moderation_is_fine() -> None:
    """一章用一次「就在这时」是正常写法，频了才是分镜脚本。"""
    text = _body(20) + "\n\n就在这时，门开了。"
    assert "transition_filler" not in _rules(aivoice.check_node(text))


def test_dialogue_ratio_is_reported() -> None:
    """840 字全是叙述、一句对白都没有：读起来就是大纲。"""
    assert "dialogue_ratio" in _rules(aivoice.check_node(_body(40)))


def test_dialogue_ratio_is_fine() -> None:
    """同样的叙述量，塞进四轮对白（约 16%）就不该报。"""
    text = _body(40) + "\n\n" + "\n\n".join([TALK_LINE] * 4)
    assert "dialogue_ratio" not in _rules(aivoice.check_node(text))


def test_short_section_skips_dialogue_ratio() -> None:
    """过场节本来就常常没人说话：800 字以下不评对白占比。"""
    assert "dialogue_ratio" not in _rules(aivoice.check_node(_body()))


def test_code_block_is_ignored() -> None:
    """技术类正文里的代码块不算行文：里面堆满破折号也不该报。"""
    text = _body() + "\n\n```\n" + "——" * 10 + "\n```"
    assert aivoice.check_node(text) == []


def test_every_rule_is_known() -> None:
    """返回的 rule 必须落在清单里：前端 RULE_LABEL 按名映射中文。"""
    text = ("\n\n".join([TAIL_LINE] * 16) + "\n\n"
            "他缓缓起身，心里五味杂陈，空气中弥漫着酒味。")
    got = _rules(aivoice.check_node(text))
    assert got and got <= set(aivoice.AVOICE_RULES)


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def _seed(store: StudioStore) -> tuple[dict, dict]:
    work = await store.create_work(kind="novel", title="AI 味接入", scale="single")
    await store.create_nodes_batch(work_id=work["id"], kind="chapter", sections=[
        {"title": "第 1 章", "content": "\n\n".join([TAIL_LINE] * 16)}])
    node = (await store.list_tree(work["id"]))[0]
    return work, await store.get_node(node["id"])


async def test_novel_checks_report_ai_voice(store: StudioStore) -> None:
    work, node = await _seed(store)
    found = await consistency.run_checks(store, work, node)
    assert "tail_summary" in {i["rule"] for i in found}


async def test_document_checks_skip_ai_voice(store: StudioStore) -> None:
    """同一段正文只把作品类型换成 article：AI 味这套不该跑。"""
    work, node = await _seed(store)
    found = await consistency.run_checks(store, {**work, "kind": "article"}, node)
    assert not ({i["rule"] for i in found} & set(aivoice.AVOICE_RULES))
