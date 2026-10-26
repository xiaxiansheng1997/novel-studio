"""软规则：命中要准，误报要零——它们只出 info，绝不能污染分数与定稿判断。

这批用例里「不报」的分支比「报」的分支更重要：软规则一旦吵人，
作者就会把整组告警关掉，那硬规则也就没人看了。
"""
from __future__ import annotations

import json

import pytest

from app.studio import consistency, scoring, soft_rules
from app.studio.store import StudioStore


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def make_novel(store, *, title="测试书") -> dict:
    work = await store.create_work(kind="novel", title=title)
    return work


async def add_chapter(store, work_id: str, ordinal: int, content: str) -> dict:
    node = await store.create_node(work_id=work_id, kind="chapter",
                                   title=f"第{ordinal}章", content=content,
                                   ordinal=ordinal)
    return node


# ==================== 中文数字 ====================

@pytest.mark.parametrize("raw,want", [
    ("35", 35), ("一", 1), ("十", 10), ("二十三", 23), ("廿七", 27),
    ("四十", 40), ("一百二十五", 125), ("三十五", 35), ("十五", 15),
])
def test_cn_to_int_accepts_plain_and_chinese(raw: str, want: int) -> None:
    assert soft_rules.cn_to_int(raw) == want


@pytest.mark.parametrize("bad", ["", "岁", "abc", "六七八九", "一百二"])
def test_cn_to_int_rejects_garbage(bad: str) -> None:
    """「一百二」这种省略单位的说法口语里是 120、字面是 102，不认。"""
    assert soft_rules.cn_to_int(bad) is None


# ==================== 称呼漂移 ====================

async def test_address_drift_flags_unregistered_title(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    node = await add_chapter(store, work["id"], 1,
                             "林公子，里边请。" + "夜风很凉。" * 10 + "明儿林公子还来不来。")
    got = await soft_rules.check_address_drift(store, work, node,
                                               node["content"], 1)
    assert len(got) == 1
    assert got[0]["severity"] == "info"
    assert "林公子" in got[0]["message"] and "林昭" in got[0]["message"]


async def test_address_drift_skips_known_alias(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭",
                              aliases=["林公子"])
    node = await add_chapter(store, work["id"], 1,
                             "林公子起身。" * 6 + "林公子点头。")
    assert await soft_rules.check_address_drift(store, work, node, node["content"], 1) == []


async def test_address_drift_needs_a_clean_boundary(store) -> None:
    """「武林公子」里的「林公子」前接汉字，不能算称呼——这是本条规则的命门。"""
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    text = "那武林公子的随从不少，另一个武林公子也来了。" + "灯火未熄。" * 20
    node = await add_chapter(store, work["id"], 1, text)
    assert await soft_rules.check_address_drift(store, work, node, text, 1) == []


async def test_address_drift_skips_ambiguous_surname(store) -> None:
    """同姓两个人时不猜「林公子」指谁。"""
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    await store.upsert_entity(work["id"], kind="character", name="林渊", canonical="林渊")
    text = "林公子到了。" + "茶凉了两回。" * 20 + "林公子又走。"
    node = await add_chapter(store, work["id"], 1, text)
    assert await soft_rules.check_address_drift(store, work, node, text, 1) == []


async def test_address_drift_reports_given_name_forms(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="沈知远", canonical="沈知远")
    text = "阿远，别闹。" + "院里安静。" * 20 + "远哥说得是。"
    node = await add_chapter(store, work["id"], 1, text)
    got = await soft_rules.check_address_drift(store, work, node, text, 1)
    assert [i["rule"] for i in got] and all("沈知远" in i["message"] for i in got)


# ==================== 设定冲突（年龄） ====================

async def test_age_mention_requires_single_anchor(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    await store.upsert_entity(work["id"], kind="character", name="周叙", canonical="周叙")
    pool = soft_rules._character_pool(await store.list_entities(work["id"]), [])
    # 两个人在同一句里，岁数归谁都不知道 → 不采
    assert soft_rules._age_mentions("林昭和周叙都是二十三岁。", pool) == []
    got = soft_rules._age_mentions("林昭今年二十三岁。", pool)
    assert [g[:2] for g in got] == [("林昭", 23)]


async def test_age_below_card_value_is_reported(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    await store.upsert_card(work["id"], kind="character", name="林昭",
                            data_json=json.dumps({"age": 25, "status_ordinal": 1}))
    text = "林昭今年二十一岁。" + "雪落满院。" * 20
    node = await add_chapter(store, work["id"], 3, text)
    got = await soft_rules.check_age_against_card(store, work, node, text, 3)
    assert len(got) == 1 and "21 岁" in got[0]["message"]


async def test_age_above_card_value_is_allowed(store) -> None:
    """写大是过日子，不能报。"""
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    await store.upsert_card(work["id"], kind="character", name="林昭",
                            data_json=json.dumps({"age": 25, "status_ordinal": 1}))
    text = "林昭已经三十二岁了。" + "雪落满院。" * 20
    node = await add_chapter(store, work["id"], 9, text)
    assert await soft_rules.check_age_against_card(store, work, node, text, 9) == []


async def test_card_age_only_applies_from_its_own_ordinal(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    await store.upsert_card(work["id"], kind="character", name="林昭",
                            data_json=json.dumps({"age": 25, "status_ordinal": 8}))
    text = "那年林昭十九岁。" + "雪落满院。" * 20
    node = await add_chapter(store, work["id"], 2, text)
    assert await soft_rules.check_age_against_card(store, work, node, text, 2) == []


async def test_age_regression_across_chapters_reported_on_later_node(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    n2 = await add_chapter(store, work["id"], 2, "林昭今年二十八岁。" + "灯下算账。" * 20)
    n5 = await add_chapter(store, work["id"], 5, "林昭今年二十五岁。" + "灯下算账。" * 20)
    got = await soft_rules.check_age_across_nodes(store, work)
    assert len(got) == 1
    # 挂在「变小那一节」，作者在这一节写作时就能看到
    assert got[0]["node_id"] == n5["id"] and got[0]["location"] == "5"
    assert n2["id"] != n5["id"]


async def test_age_progression_across_chapters_is_clean(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    await add_chapter(store, work["id"], 2, "林昭今年二十五岁。" + "灯下算账。" * 20)
    await add_chapter(store, work["id"], 5, "林昭今年二十八岁。" + "灯下算账。" * 20)
    assert await soft_rules.check_age_across_nodes(store, work) == []


# ==================== 绝对化表述 / 数字无出处 ====================

async def test_absolute_claim_hits(store) -> None:
    work = await store.create_work(kind="article", title="推荐", subtype="lifestyle")
    text = "这是最好的选择，也是唯一值得买的。" + "其余几句普通的话。" * 20
    node = await add_chapter(store, work["id"], 1, text)
    got = await soft_rules.check_absolute_claim(store, work, node, text, 1)
    assert got and got[0]["rule"] == "absolute_claim"
    assert len(got) <= soft_rules.MAX_PER_RULE


async def test_absolute_claim_skips_plain_words(store) -> None:
    work = await store.create_work(kind="article", title="推荐", subtype="lifestyle")
    text = "价格不错，用着省心。" + "日常细节也够。" * 20
    node = await add_chapter(store, work["id"], 1, text)
    assert await soft_rules.check_absolute_claim(store, work, node, text, 1) == []


async def test_absolute_claim_skips_ordinal_first(store) -> None:
    """「第一步」「第一类」是序号，「唯一凭据」是普通用法。收裸词之前的误报全部来自这里。"""
    work = await store.create_work(kind="article", title="推荐", subtype="lifestyle")
    text = ("第一步：装依赖。第二类是索引丢失。第一，输入名要核对。"
            "这个号是后续沟通的唯一凭据，它也是唯一的缺点。" + "其余是普通句子。" * 20)
    node = await add_chapter(store, work["id"], 1, text)
    assert await soft_rules.check_absolute_claim(store, work, node, text, 1) == []
    # 但带领域的吹嘘还是要报
    text2 = "这是销量第一的选择。" + "其余是普通句子。" * 20
    got = await soft_rules.check_absolute_claim(store, work, node, text2, 1)
    assert len(got) == 1
    text3 = "这是唯一值得买的。" + "其余是普通句子。" * 20
    assert len(await soft_rules.check_absolute_claim(store, work, node, text3, 1)) == 1


async def test_number_unsourced_hits_and_misses(store) -> None:
    work = await store.create_work(kind="article", title="报告", subtype="report")
    await store.add_source(work["id"], content="同比增长 18.6%，规模 3.2 亿元。")
    ok_text = "同比增长 18.6%，规模 3.2 亿元。" + "其余分析。" * 20
    node = await add_chapter(store, work["id"], 1, ok_text)
    assert await soft_rules.check_number_unsourced(store, work, node, ok_text, 1) == []

    bad_text = "到 2027 年销量将达 120 万台。" + "其余分析。" * 20
    node2 = await add_chapter(store, work["id"], 2, bad_text)
    got = await soft_rules.check_number_unsourced(store, work, node2, bad_text, 2)
    assert {i["message"][:12] for i in got} and len(got) == 2  # 2027 年 / 120 万台


async def test_number_unsourced_silent_without_corpus(store) -> None:
    """没登记素材/事实卡就整条闭嘴：报「全都无出处」只会让人关掉告警。"""
    work = await store.create_work(kind="article", title="报告", subtype="report")
    text = "规模达到 120 万台，同比 18.6%。" + "其余分析。" * 20
    node = await add_chapter(store, work["id"], 1, text)
    assert await soft_rules.check_number_unsourced(store, work, node, text, 1) == []


async def test_number_unsourced_skips_technical_values(store) -> None:
    """不带单位的裸数字、行内代码里的数字不算「数据」。"""
    work = await store.create_work(kind="article", title="报告", subtype="report")
    await store.add_source(work["id"], content="同比增长 18.6%。")
    text = ("默认端口 8000，返回 `503`，版本 1.18.0，重试 3 次。" + "其余分析。" * 20)
    node = await add_chapter(store, work["id"], 1, text)
    assert await soft_rules.check_number_unsourced(store, work, node, text, 1) == []


# ==================== 与打分/配置的边界 ====================

async def test_soft_issues_never_cost_score(store) -> None:
    """软规则必须是 info：一旦进扣分链路，批量跑分会被噪音拖花。"""
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    text = "林公子起身，林公子落座。" + "夜风很凉。" * 30
    node = await add_chapter(store, work["id"], 1, text)
    issues = await soft_rules.check_node(store, work, node, text, 1,
                                         set(soft_rules.NOVEL_SOFT))
    assert issues and all(i["severity"] == "info" for i in issues)
    scored = scoring.score_node(node, text, kind="novel", target_words=200, issues=issues)
    assert scored["parts"]["compliance"] == 1.0


async def test_soft_rules_off_when_checks_not_enabled(store) -> None:
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    text = "林公子起身，林公子落座。" + "夜风很凉。" * 30
    node = await add_chapter(store, work["id"], 1, text)
    assert await soft_rules.check_node(store, work, node, text, 1, set()) == []
    assert await soft_rules.check_whole_book(store, work, set()) == []


async def test_run_checks_writes_soft_issues_into_store(store) -> None:
    """走一遍 consistency 入口：配置开着才会落库，前端才有东西可看。"""
    work = await make_novel(store)
    await store.upsert_entity(work["id"], kind="character", name="林昭", canonical="林昭")
    text = "林公子起身，林公子落座。" + "夜风很凉。" * 30
    node = await add_chapter(store, work["id"], 1, text)
    resolved = {"checks": ["address_drift"]}
    created = await consistency.run_checks(store, work, node, resolved=resolved)
    assert [c["rule"] for c in created] == ["address_drift"]
    rows = await store.list_issues(work["id"], node_id=node["id"], only_open=True)
    assert len(rows) == 1


async def test_default_novel_config_enables_both_soft_rules() -> None:
    from app.studio import content_types

    checks = set(content_types.resolve("novel", "single", "")["checks"])
    assert set(soft_rules.NOVEL_SOFT) <= checks
    for subtype in ("history", "politics", "report"):
        got = set(content_types.resolve("article", "single", subtype)["checks"])
        assert "number_unsourced" in got, subtype
    assert "absolute_claim" in set(content_types.resolve("article", "single",
                                                         "lifestyle")["checks"])
