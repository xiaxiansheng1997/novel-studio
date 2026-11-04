"""审校规则（proofread）：命中要准，护栏要比命中更严。

这批用例一半在测「不报」：半角标点对上小数/版本号、引号对上跨段对话、
连打上对上半角强调、叠字对上「的的确确」——审校一旦吵人，作者会连
硬告警一起关掉，那损失比漏报一条排印错误大得多。
另外两条链路级断言：warn 必须能落进 issues 表，但**不能拦定稿**。
"""
from __future__ import annotations

import pytest

from app.api.studio_routes import _final_gate
from app.studio import consistency, proofread
from app.studio.store import StudioStore

PAD = "夜风很凉。" * 25          # 让节稳过 80 字的起判门槛


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


def hit(out: list[dict], rule: str) -> list[dict]:
    return [i for i in out if i["rule"] == rule]


# ==================== 半角标点 ====================

def test_halfwidth_hits_chinese_glued() -> None:
    out = proofread.check_node("他说，今天很好,我们明天见。" + PAD, 1)
    got = hit(out, "halfwidth_punct")
    assert len(got) == 1 and got[0]["severity"] == "warn"
    assert "好," in got[0]["message"]


def test_halfwidth_skips_numbers_english_and_urls() -> None:
    text = ("产量增长了3.5万吨，版本升到v1.2吧。他说 Hello, world. "
            "再看 https://a.com/x,y 的下载页，字体是 18px 的。" + PAD)
    assert hit(proofread.check_node(text, 1), "halfwidth_punct") == []


def test_halfwidth_flags_trailing_dot() -> None:
    """中文句子用半角点收尾：后面没字母没数字，该报。"""
    assert hit(proofread.check_node("今天的事就说这些." + PAD, 1), "halfwidth_punct")


# ==================== 符号配对 ====================

def test_quotes_balanced_across_paragraphs() -> None:
    """多段对话各段自带开闭，整节计数平衡——不该误报。"""
    text = '甲：“今天事多。”\n\n乙：“还有明天。”\n\n甲：“总之先这样。”\n' + PAD
    assert hit(proofread.check_node(text, 1), "punct_unbalanced") == []


def test_unbalanced_quote_reported_with_counts() -> None:
    out = hit(proofread.check_node('他说：“今天好。' + PAD, 1), "punct_unbalanced")
    assert len(out) == 1 and out[0]["severity"] == "warn"
    assert "缺闭" in out[0]["message"]


def test_unbalanced_bracket_reported() -> None:
    out = hit(proofread.check_node("先看（注：后文再说。" + PAD, 1), "punct_unbalanced")
    assert out and "（）" in out[0]["message"]


# ==================== 标点连打 ====================

def test_cluster_hits_and_emphasis_passes() -> None:
    out = proofread.check_node("走吧，。现在吃饭。" + PAD, 1)
    got = hit(out, "punct_cluster")
    assert len(got) == 1 and "，。" in got[0]["message"]
    # 「？！」组成的是强调，「。。」该写成「……」——前者不报后者报
    assert hit(proofread.check_node("真的吗？？她没答。" + PAD, 1), "punct_cluster") == []
    dot = hit(proofread.check_node("他转身。。走了两步。" + PAD, 1), "punct_cluster")
    assert dot and "……" in dot[0]["message"]


# ==================== 叠字残留 ====================

def test_dup_char_hits_function_words() -> None:
    out = hit(proofread.check_node("他说得的的确不是这样。" + PAD, 1), "char_repeat")
    assert len(out) == 1 and out[0]["severity"] == "warn"


def test_dup_char_skips_idioms_and_reduplication() -> None:
    """白名单成语与实词叠字（谢谢/刚刚/看看）都不能报。"""
    text = "他说得的的确确，她也谢谢刚刚来看看的张哥。是是非非都过去了。" + PAD
    assert hit(proofread.check_node(text, 1), "char_repeat") == []


# ==================== 文风三条（info） ====================

def test_long_sentence_info() -> None:
    long = "他一边说着一边写着根本停不下来" * 6      # 84 字还没到线
    assert hit(proofread.check_node(long + "。" + PAD, 1), "long_sentence") == []
    long2 = "他一边说着一边写着根本停不下来" * 12 + "。"
    got = hit(proofread.check_node(long2, 1), "long_sentence")
    assert len(got) == 1 and got[0]["severity"] == "info"


def test_de_density_needs_long_para() -> None:
    dense = "我的新的旧的" * 40                       # 240 字里 120 个「的」
    sent = "。".join(dense[i:i + 60] for i in range(0, len(dense), 60)) + "。"
    assert hit(proofread.check_node(sent, 1), "de_density")
    # 短段落不评：密度再高也先问段长
    assert hit(proofread.check_node("我的新的旧的真的很好。" + PAD, 1), "de_density") == []


def test_long_paragraph_info() -> None:
    para = "。".join("夜风很凉灯也暗" for _ in range(40)) + "。"   # 360 字一句一行段
    assert hit(proofread.check_node(para, 1), "long_paragraph") == []
    big = "。".join("夜风很凉灯也暗路上没人说话" for _ in range(100)) + "。"
    got = hit(proofread.check_node(big, 1), "long_paragraph")
    assert len(got) == 1 and got[0]["severity"] == "info"


# ==================== 门槛与上限 ====================

def test_short_section_is_not_judged() -> None:
    assert proofread.check_node("这里,有问题。", 1) == []


def test_code_block_is_excluded() -> None:
    text = "下面这段是代码。\n```\nfoo(a,b);\nprint 1.2;\n```\n" + PAD
    assert hit(proofread.check_node(text, 1), "halfwidth_punct") == []


def test_each_rule_capped_per_section() -> None:
    noisy = "他说，这里有问题,那里也有。\n" * 8 + PAD
    out = proofread.check_node(noisy, 1)
    assert len(hit(out, "halfwidth_punct")) <= proofread.MAX_PER_RULE


# ==================== 落库与定稿链路 ====================

async def test_run_checks_lands_proofread_issues(store) -> None:
    """审校是 always-on：`checks` 开关为空也得跑（渠道层是覆盖式写法）。"""
    work = await store.create_work(kind="novel", title="审校测试")
    bad = "他说，今天很好,我们明天见。" + PAD + "真的,就这样。"
    node = await store.create_node(work_id=work["id"], kind="chapter",
                                   title="第1章", content=bad, ordinal=1)
    created = await consistency.run_checks(store, work, node, resolved={"checks": []})
    rules = {i["rule"] for i in created}
    assert "halfwidth_punct" in rules
    # add_issue 只回 id/rule/severity/message；node_id 要回库里验
    rows = await store.list_issues(work["id"], node_id=node["id"], limit=100)
    half = next(r for r in rows if r["rule"] == "halfwidth_punct")
    assert half["severity"] == "warn" and half["node_id"] == node["id"]


async def test_fixed_content_clears_previous_issues(store) -> None:
    work = await store.create_work(kind="novel", title="审校测试")
    bad = "他讲的的没错,就是这样。" + PAD
    node = await store.create_node(work_id=work["id"], kind="chapter",
                                   title="第1章", content=bad, ordinal=1)
    await consistency.run_checks(store, work, node, resolved={"checks": []})
    assert await store.list_issues(work["id"], only_open=True, limit=100)

    clean = "\n".join(["夜风很凉。"] * 25)
    await store.update_node(node["id"], content=clean)
    fresh = await store.get_node(node["id"])
    await consistency.run_checks(store, work, fresh, resolved={"checks": []})
    assert await store.list_issues(work["id"], only_open=True, limit=100) == []


async def test_warn_never_blocks_final_gate(store) -> None:
    """四条 warn 都是排印瑕疵：定稿闸门只认 error，一条都不该拦。"""
    work = await store.create_work(kind="novel", title="审校测试")
    bad = "他说，今天很好,我们明天见。走吧，。吃饭。" + PAD
    node = await store.create_node(work_id=work["id"], kind="chapter",
                                   title="第1章", content=bad, ordinal=1)
    created = await consistency.run_checks(store, work, node, resolved={"checks": []})
    assert created and all(i["severity"] != "error" for i in created)
    gate = await _final_gate(store, node)
    assert gate["open_errors"] == 0
