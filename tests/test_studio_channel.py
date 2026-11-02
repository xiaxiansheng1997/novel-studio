"""渠道（channels）是与文种正交的第二个维度。

要守住的三件事：

- 叠渠道不能把「文档 / 历史」顶成「公众号」：label、预算、层级都归文种管，
  渠道只叠写法与检查项。「历史 + 公众号」是合法组合，两个维度都要在场。
- 渠道的写法不拼进 `style_prompt`，而是进 `channel_style_prompt` /
  `channel_instructions`：装配层要靠这两个键加「平台要求（公众号）：」前缀，
  提前拼在一起，那层区分就再也拆不回来了。
- checks 合并后去重：文种与渠道挂着同一条规则时只出现一次。
"""
from __future__ import annotations

import pytest

from app.studio import content_types

# 一份最小 YAML：故意让渠道与文种挂同一条检查规则，用来验证去重
FAKE = """
version: 1
kinds:
  article:
    label: 文档
    budget_chars: 9000
    style_prompt: 文种写法
    checks: [structure_incomplete]
    layers:
      - { kind: section, label: 节, readable: true }
    scales:
      single: { label: 单篇 }
    subtypes:
      history:
        label: 历史
        template: tpl_x
        checks_append: [citation_missing]
        instructions: 区分史料与推断
channels:
  short:
    label: 短渠道
    target_words: 600
    style_prompt: 平台写法
    instructions: 平台约束
    checks_append: [citation_missing, absolute_claim]
templates: []
"""


@pytest.fixture
def fake_types(tmp_path, monkeypatch):
    """把 content-types.yaml 换成临时文件，测完恢复真实配置。

    `get_types()` 有 mtime 缓存，所以切路径之后必须 `reload_types()`，
    否则读到的还是上一份。
    """
    path = tmp_path / "content-types.yaml"
    path.write_text(FAKE, encoding="utf-8")
    monkeypatch.setenv("W2A_CONTENT_TYPES", str(path))
    content_types.reload_types()
    try:
        yield path
    finally:
        monkeypatch.delenv("W2A_CONTENT_TYPES", raising=False)
        content_types.reload_types()


def test_channel_adds_writing_rules_without_stealing_the_label(fake_types) -> None:
    got = content_types.resolve("article", "single", "history", "short")

    # 文种维度：subtype 的 label 可以覆盖 kind 的（「历史」），但渠道不能把它顶成「短渠道」
    assert got["label"] == "历史"
    assert got["subtype_label"] == "历史"
    assert got["budget_chars"] == 9000
    assert got["leaf_label"] == "节"
    assert got["style_prompt"] == "文种写法"
    assert got["instructions"] == "区分史料与推断"

    # 渠道维度：写法进独立键，不混进 style_prompt
    assert got["channel"] == "short"
    assert got["channel_label"] == "短渠道"
    assert got["channel_target_words"] == 600
    assert got["channel_style_prompt"] == "平台写法"
    assert got["channel_instructions"] == "平台约束"

    # checks 三层 + 渠道合并后去重，citation_missing 只出现一次
    assert got["checks"] == ["structure_incomplete", "citation_missing", "absolute_claim"]


def test_no_channel_means_no_platform_block(fake_types) -> None:
    got = content_types.resolve("article", "single", "history")

    assert got["channel"] == ""
    assert got["channel_label"] == ""
    assert got["channel_target_words"] == 0
    assert got["channel_style_prompt"] == ""
    assert got["channel_instructions"] == ""
    # 不带渠道时平台那段整段不出现，不能给模型一个空标题
    assert "平台要求" not in content_types.requirements_text(got)


def test_unknown_channel_falls_back_to_the_id(fake_types) -> None:
    """YAML 里没配的渠道名不该炸，也不该冒出半段平台要求。"""
    got = content_types.resolve("article", "single", "history", "nope")

    assert got["channel"] == "nope"
    assert got["channel_label"] == "nope"  # 退成 id：界面上还看得出选了什么
    assert got["channel_style_prompt"] == ""
    assert got["checks"] == ["structure_incomplete", "citation_missing"]


def test_requirements_text_keeps_the_two_dimensions_apart(fake_types) -> None:
    """一次性任务（改写/压缩）要一段汇总文本：文体在前，平台在后。"""
    got = content_types.resolve("article", "single", "history", "short")
    text = content_types.requirements_text(got)

    assert text.startswith("文体要求：")
    assert "平台要求（短渠道）：" in text
    assert text.index("文体要求") < text.index("平台要求")
    # 文种的要求不许跑到平台那一段里
    assert text.split("平台要求（短渠道）：")[0].count("平台写法") == 0


def test_shipped_yaml_has_channels_and_the_history_wechat_combo() -> None:
    """随包那份 YAML 的冒烟：渠道段在，且「历史 + 公众号」取得出来。

    wechat 曾经是 subtypes 里的一档，与 history 互斥；这条测的就是那次拆分。
    """
    content_types.reload_types()
    ids = {c["id"] for c in content_types.list_channels()}
    assert {"wechat", "zhihu", "xiaohongshu", "toutiao", "blog", "official"} <= ids

    got = content_types.resolve("article", "single", "history", "wechat")
    assert got["channel_label"] == "公众号"
    assert got["subtype_label"] == "历史"
    assert "每段不超过三行" in got["channel_style_prompt"]
    assert "[图：" in got["channel_instructions"]
    # 公众号带来的 absolute_claim 与历史自己的两条并在一起
    assert {"citation_missing", "number_unsourced", "absolute_claim"} <= set(got["checks"])
    assert len(got["checks"]) == len(set(got["checks"]))

    # 旧的「公众号」文种已经不存在了：再选一次只会得到 unknown 的 label
    article = next(k for k in content_types.list_kinds() if k["id"] == "article")
    assert "wechat" not in {s["id"] for s in article["subtypes"]}
