"""检索 query 的取词口径：模板句不该占 MATCH 的名额。

原来的写法是把 `node_instruction()` 整段交给分词、取前 12 个词做 OR。
一节 instruction 有 123 个词，「本节标题」「篇幅：约 1000 字」「只输出本节正文」
这些模板句占了大半——实测前 12 名里一半是套话，而计划里真正的地名/器物
（「西市」「暗格」「令底」）一个都进不去。检索不是没召回，是根本没问出去。

现在 `prompting.retrieval_terms()` 按重要度取词（标题 → 地点/时间 → 要点 →
节拍卡 → 附加要求），`match_query` 把上限提到 24，并在**检索路径**上剔除模板词
（`drop_template=True`）——跨作品搜索走同一个函数，那边必须保留。
"""
from __future__ import annotations

import json

from app.studio import prompting, retrieval, tokenize

# 本书术语表：生产里由 indexer.reindex_work 灌进 jieba，这里手动补上，
# 否则「西市仓库」「令底」会被切碎，测的就不是取词逻辑而是词典命中
tokenize.add_words(["林昭", "周叙", "西市仓库", "青岚仓库", "令底", "偏位", "拓纹", "玄铁令"])

META: dict = {}


def make_node(**over) -> dict:
    node = {
        "id": "n_2",
        "title": "第 2 章 夜访",
        "ordinal": 2,
        "summary": (
            "出场：林昭、周叙\n地点：西市仓库\n事件：\n"
            "- 灶房的暗格被撬开\n- 拓纹显示令底偏位两分"
        ),
        "plan_json": json.dumps({
            "prompt": "林昭夜访西市仓库，在灶房暗格里翻出令底偏位的拓纹，与周叙约定三号再碰",
            "place": "西市仓库",
            "time": "子夜",
            "pov": "第三人称",
            "target_words": 1000,
        }, ensure_ascii=False),
    }
    node.update(over)
    return node


# ==================== 取词：计划和节拍卡里的专名要排在前面 ====================

def test_plan_terms_come_from_place_and_prompt() -> None:
    terms = prompting.retrieval_terms(make_node(), META)
    joined = " ".join(terms)
    for word in ("西市仓库", "令底", "偏位", "拓纹", "夜访", "子夜"):
        assert word in terms, f"{word} 没进检索词表: {terms}"
    # 叙事视角是格式约束不是检索线索；章节序号全库都有
    assert "第三人称" not in joined
    assert "第" not in terms and "章" not in terms


def test_ordinal_is_stripped_before_tokenizing() -> None:
    """「第 12 章」整段抠掉：jieba 会把「第一章」切成「第一」「章」，事后过滤只拦得住后者。"""
    node = make_node(title="第 12 章 夜访", plan_json="{}", summary="")
    terms = prompting.retrieval_terms(node, META)
    assert "夜访" in terms
    assert not any(t.startswith("第") for t in terms), terms


def test_legacy_outline_still_supplies_terms() -> None:
    """没填节点计划的老书：检索词要能从 works.meta_json.outline 兜底。"""
    node = make_node(plan_json="{}", summary="")
    meta = {"outline": {node["id"]: {"prompt": "写西市仓库那场夜访"}}}
    terms = prompting.retrieval_terms(node, meta)
    assert "西市仓库" in terms and "夜访" in terms


def test_extra_line_is_appended_after_the_plan() -> None:
    """用户临时写的附加要求也要进检索，但不能把计划挤到后面。"""
    terms = prompting.retrieval_terms(make_node(), META, extra="结尾点一下玄铁令")
    assert terms.index("西市仓库") < terms.index("玄铁令"), terms


def test_instruction_template_does_not_crowd_out_plan_terms() -> None:
    """这条是本次修复的靶心：把整段 instruction 交给分词，地名/器物必须仍在。"""
    node = make_node()
    instruction = prompting.node_instruction(node, META)
    terms = prompting.retrieval_terms(node, META, extra=instruction)
    query = retrieval.build_query(*terms, "一部关于玄铁令的小说")
    # retrieve 走的就是 drop_template=True 这条
    match = tokenize.match_query(query, drop_template=True)

    for word in ("西市仓库", "令底", "偏位", "拓纹"):
        assert f'"{word}"' in match, f"{word} 被模板句挤出 MATCH: {match}"
    assert '"标题"' not in match and '"篇幅"' not in match


# ==================== MATCH 表达式 ====================

def test_match_query_drops_template_words_and_caps_at_24() -> None:
    filler = [f"term{i:02d}" for i in range(30)]
    match = tokenize.match_query("本节 标题 要点 篇幅 " + " ".join(filler), drop_template=True)
    for noise in ("本节", "标题", "要点", "篇幅"):
        assert f'"{noise}"' not in match, match
    assert match.count(" OR ") + 1 == 24, match


def test_match_query_keeps_template_words_by_default() -> None:
    """跨作品搜索共用这个函数：手敲「正文」就得搜正文，不能剔成空。"""
    assert tokenize.match_query("正文") == '"正文"'
    match = tokenize.match_query("正文 玄铁令")
    assert '"正文"' in match and '"玄铁令"' in match


def test_match_query_falls_back_when_everything_is_template() -> None:
    """整句都是模板词时退回不过滤的切法：宁可噪声大一点，不能把查询打成空。"""
    match = tokenize.match_query("正文 标题", drop_template=True)
    assert match, "全被剔掉就该兜底，返回空等于什么都没搜"
    assert '"正文"' in match


def test_match_query_keeps_short_queries_intact() -> None:
    """跨作品搜索传一句短语：模板词表不能顺手把用户真搜的词丢掉。"""
    assert tokenize.match_query("细节") == '"细节"'
    assert tokenize.match_query("") == ""
