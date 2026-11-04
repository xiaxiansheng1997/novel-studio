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
    # 自我重复挂两条线：小说按章复用会重复，系列文档按讲复用同样会重复
    assert "phrase_repeat" in checks
    assert "phrase_repeat" in set(content_types.resolve("article", "series",
                                                         "tech")["checks"])
    assert "phrase_repeat" in set(soft_rules.BOOK_SOFT)


# ==================== 自我重复（跨节） ====================

async def _novel_with(store, texts: list[str]) -> tuple[dict, list[dict]]:
    """一本 N 章的书：逐章正文由用例给，标题与序号自动生成。"""
    work = await make_novel(store)
    nodes = [await add_chapter(store, work["id"], i, t) for i, t in enumerate(texts, 1)]
    return work, nodes


async def test_phrase_repeat_flags_a_line_used_across_chapters(store) -> None:
    """同一句在四章里用了三次——往后两章的作者会以为自己复制粘贴了。"""
    work, nodes = await _novel_with(store, [
        "雪落了满山，她把披风紧了紧，继续往山下走。风把脚印一点点填平。",
        "雪落了满山，镇口那盏灯却一盏也没亮起来。她数了三遍门牌。",
        "雪落了满山，他站在门外听了很久的动静。屋里没有人回答他。",
        "春汛来了，河面浮着碎冰，船工都不肯下水。岸上的人越聚越多。",
    ])
    got = await soft_rules.check_phrase_repeat(store, work)
    assert got and all(i["rule"] == "phrase_repeat" for i in got)
    assert all(i["severity"] == "info" for i in got)
    hit = next((i for i in got if "雪落了满山" in i["message"]), None)
    assert hit, [i["message"] for i in got]
    # 挂在最后一次出现的那一节上，作者正在写的就是那里
    assert hit["location"] == "3" and hit["node_id"] == nodes[2]["id"]


async def test_phrase_repeat_ignores_character_names(store) -> None:
    """角色名每节都会出现，它不是句式在偷懒。"""
    work, _ = await _novel_with(store, [
        "林知微推门进去，屋里静得能听见水滴。桌上压着一封没拆的信。",
        "林知微推门进去，炉子早就凉透了。窗外的雪堆到一半高。",
        "林知微推门进去，椅子上还搭着昨天的外衣。灯芯烧短了半截。",
        "林知微推门进去，地上一层薄灰。墙角放着没洗的茶盏。",
    ])
    # 先确认这七个字真会被当成候选：否则「排除生效」只是规则没跑起来
    before = await soft_rules.check_phrase_repeat(store, work)
    assert any("林知微推门进去" in i["message"] for i in before), \
        [i["message"] for i in before]
    await store.upsert_entity(work["id"], kind="character", name="林知微", canonical="林知微")
    got = await soft_rules.check_phrase_repeat(store, work)
    assert not any("林知微" in i["message"] for i in got), [i["message"] for i in got]
    assert got == []


async def test_phrase_repeat_ignores_terminology(store) -> None:
    """术语本来就该每一讲都出现，报了就是噪音。"""
    work, _ = await _novel_with(store, [
        "灰烬之城的城门每天落日后关闭。守门人不与人多话，只点头。",
        "他从灰烬之城的城门走出去，回头看了一眼城头上的灯。",
        "那天傍晚，灰烬之城的城门开了半扇，里面没有人出来。",
    ])
    before = await soft_rules.check_phrase_repeat(store, work)
    assert any("灰烬之城的城门" in i["message"] for i in before), \
        [i["message"] for i in before]
    await store.upsert_term(work["id"], term="灰烬之城", definition="主城")
    got = await soft_rules.check_phrase_repeat(store, work)
    assert not any("灰烬之城" in i["message"] for i in got), [i["message"] for i in got]
    assert got == []


async def test_phrase_repeat_ignores_item_and_location_names(store) -> None:
    """道具、地点跨节重复是设定在起作用，不是句式在偷懒。

    真实语料里「三号暗格」在 3 节出现 5 次——它是个地点实体，不该报。
    """
    work, _ = await _novel_with(store, [
        "他提着灯走到三号暗格前，把铜锁拨开，里面空了一层。",
        "第二天他一个人又走到三号暗格前，蹲下去看地上的灰。",
        "雨夜里他绕到三号暗格前，站了很久才原路回灶房那边。",
    ])
    before = await soft_rules.check_phrase_repeat(store, work)
    assert any("三号暗格" in i["message"] for i in before), [i["message"] for i in before]
    await store.upsert_entity(work["id"], kind="location", name="三号暗格", canonical="三号暗格")
    assert await soft_rules.check_phrase_repeat(store, work) == []


async def test_phrase_repeat_skips_four_char_chunks(store) -> None:
    """四字以内的复用是正当的：成语、叠词、服饰名都在这个长度里。

    第一版判据是 4 字起，真实语料上报的就是「一字一字」「青布直裰」这类，
    没有一条是句式偷懒，所以门槛提到 5 字。
    """
    work, _ = await _novel_with(store, [
        "他把青布直裰的下摆掖进腰带，弯腰钻进了货架后的夹道。",
        "夜里风大，青布直裰实在太薄，他只好把外衣再裹一层。",
        "天亮前他换下青布直裰，叠好压在箱底，换了身短打。",
    ])
    assert await soft_rules.check_phrase_repeat(store, work) == []


async def test_phrase_repeat_ignores_repetition_inside_one_section(store) -> None:
    """一节里排比十次是正当修辞，单节内的复用不算自我重复。"""
    work, _ = await _novel_with(store, [
        "雪落了满山。" * 12 + "她没有回头，只是把披风紧了紧。",
        "灯芯烧到了尽头，屋里慢慢暗下去。有人在院子里扫地。",
        "码头上的绳子磨得发白，船家把它换了一根新的。",
        "镇口新开了一家铺子，卖些零碎的针线。掌柜姓周。",
    ])
    got = await soft_rules.check_phrase_repeat(store, work)
    assert got == []


async def test_phrase_repeat_needs_three_sections(store) -> None:
    """三节的小样本里复用两次就报，等于让作者不敢写第三章。"""
    work, _ = await _novel_with(store, [
        "雪落了满山，她把披风紧了紧，继续往山下走。风把脚印填平。",
        "雪落了满山，镇口那盏灯却一盏也没亮起来。她数了三遍门牌。",
        "春汛来了，河面浮着碎冰，船工都不肯下水。岸上的人越聚越多。",
    ])
    assert await soft_rules.check_phrase_repeat(store, work) == []


async def test_phrase_repeat_skips_code_blocks(store) -> None:
    """代码块里的同一行命令不算重复：它是粘贴的，而且本来就长这样。"""
    work = await store.create_work(kind="article", title="技术笔记", subtype="tech")
    code = "```python\nsubprocess.Popen(['uvicorn', 'app.main:app'])\n```"
    texts = [
        f"服务起不来的时候，先看端口有没有被占用。\n{code}\n然后去看启动日志。",
        f"部署到一半断网了，别急着重试，先确认进程。\n{code}\n这一步很容易看错。",
        f"本地调试直接用脚本起，省得记参数。\n{code}\n两边行为要保持一致。",
    ]
    for i, text in enumerate(texts, 1):
        await store.create_node(work_id=work["id"], kind="section", title=f"第{i}节",
                                content=text, ordinal=i)
    assert await soft_rules.check_phrase_repeat(store, work) == []


async def test_phrase_repeat_skips_fragments_around_numbers(store) -> None:
    """数字把汉字串切短了，剩下的「重试」这种碎片不该被拼成短语。"""
    work, _ = await _novel_with(store, [
        "服务挂住的时候不要再派人盯着，先重试 3 次。看日志里的时间戳。",
        "线上遇到这种情况，把客户端改成重试 3 次。别在页面上加提示。",
        "压测那天结论很直接：让它重试 3 次。再多的改动都不值得。",
    ])
    got = await soft_rules.check_phrase_repeat(store, work)
    assert not any("重试" in i["message"] for i in got), [i["message"] for i in got]


async def test_phrase_repeat_flags_identical_openings(store) -> None:
    """四章同一句起手式：读者以为是通稿。"""
    work, nodes = await _novel_with(store, [
        "天亮了，林知微推开窗，雪已经停了。她站着没动。",
        "天亮了，林知微推开窗，雪已经积到门槛上。狗在院里叫。",
        "天亮了，林知微推开窗，雪已经化了一半。屋檐在滴水。",
        "天亮了，林知微推开窗，雪已经漫过台阶。远处有人敲钟。",
    ])
    got = await soft_rules.check_phrase_repeat(store, work)
    opens = [i for i in got if "开头前 12 个字一模一样" in i["message"]]
    assert opens, [i["message"] for i in got]
    # 只在后出现的那些节上报，第一节不需要重复提醒
    assert all(i["location"] in ("2", "3", "4") for i in opens)
    assert all(i["node_id"] in {n["id"] for n in nodes[1:]} for i in opens)
    assert len(opens) <= soft_rules.MAX_PER_RULE


async def test_phrase_repeat_reports_the_longest_variant_only(store) -> None:
    """「嘴角微微上扬」和「嘴角上扬」一起报就是两条废话。"""
    work, _ = await _novel_with(store, [
        "她听完这句话，她的嘴角微微上扬，像是在听一个不好笑的笑话。",
        "他没有解释，她的嘴角微微上扬，把手里的杯子放下了。",
        "屋里安静了一会儿，她的嘴角微微上扬，转身去看窗外。",
    ])
    got = await soft_rules.check_phrase_repeat(store, work)
    hits = [i for i in got if "嘴角" in i["message"]]
    assert len(hits) == 1, [i["message"] for i in hits]
    assert "她的嘴角微微上扬" in hits[0]["message"]


async def test_phrase_repeat_caps_the_number_of_reports(store) -> None:
    """复用密集的书也不该刷屏：整条规则封顶 MAX_PER_RULE * 3。"""
    shared = ["雪落了满山", "灯亮到天明", "风吹过窗棂", "雨打湿台阶", "人走得很慢"]
    texts = ["，".join(shared) + "。余下的句子每一节都不一样，写点别的。" for _ in range(4)]
    texts = [texts[0] + "她数着门牌。", texts[1] + "狗在院里叫。",
             texts[2] + "屋檐在滴水。", texts[3] + "远处有人敲钟。"]
    work, _ = await _novel_with(store, texts)
    got = await soft_rules.check_phrase_repeat(store, work)
    assert got and len(got) <= soft_rules.MAX_PER_RULE * 3


async def test_whole_book_runs_phrase_repeat_for_documents_too(store) -> None:
    """系列文档「同一句式复用二十讲」同样成立——那道 novel 门禁已经拆了。"""
    work = await store.create_work(kind="article", title="专栏", subtype="tech")
    texts = [
        "先看现象，再看日志，最后才动代码。这个顺序不要颠倒。",
        "接着上次的结论说：先看现象，再看日志，最后才动代码。",
        "落地的第一步还是先看现象，再看日志，最后才动代码。",
    ]
    for i, text in enumerate(texts, 1):
        await store.create_node(work_id=work["id"], kind="section", title=f"第{i}讲",
                                content=text, ordinal=i)
    got = await soft_rules.check_whole_book(store, work, {"phrase_repeat"})
    assert got and all(i["rule"] == "phrase_repeat" for i in got)
    assert await soft_rules.check_whole_book(store, work, set()) == []
