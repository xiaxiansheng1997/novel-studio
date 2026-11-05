"""采样档位表（app.studio.sampling）的回归测试。

这张表本身没有逻辑可言，值得测的是两件事：
1. 方向不能反——拆解要比正文更收敛，反过来就会看到 JSON 字段开始编故事；
2. 任务类型不能漏——漏掉一个类型就等于悄悄用回上游默认温度，而这种事
   在日志里完全看不出来。第二个用「与 REGISTRY 对齐」的守卫测试兜住。
"""
from __future__ import annotations

from app.studio import sampling, tasks


def test_creative_is_hotter_than_strict() -> None:
    """写正文要比抽取更发散：这条方向反了就是产品事故，不是风格偏好。"""
    assert sampling.for_task("draft")["temperature"] > sampling.for_task("summarize")["temperature"]
    assert sampling.for_task("polish")["temperature"] > sampling.for_task("extract")["temperature"]


def test_beats_sits_between_beats_and_prose() -> None:
    """节拍卡是「定这几件事」，比正文低、比抽取高。"""
    beats = sampling.for_task("beats")["temperature"]
    assert sampling.for_task("craft")["temperature"] < beats < sampling.for_task("draft")["temperature"]


def test_craft_asks_for_json_mode() -> None:
    """拆解的输出契约就是 JSON：这一条是 P0 里对本地小模型最实在的收益。"""
    assert sampling.for_task("craft") == {"temperature": sampling.TEMP_STRICT, "json_mode": True}


def test_check_is_strict_but_not_json() -> None:
    """check 要收敛，但它的产出是给人读的问题清单，不该被上游逼成 JSON。"""
    assert sampling.for_task("check") == {"temperature": sampling.TEMP_STRICT}


def test_unknown_task_gets_nothing() -> None:
    """认不得的类型宁可沿用上游默认，也别替调用方瞎猜温度。"""
    assert sampling.for_task("something_new") == {}


def test_every_queued_task_type_is_classified() -> None:
    """新增任务类型必须在这张表里表态。

    漏配不会报错，只会静默地用上游默认温度——这类「不失败的退化」最难查，
    所以拿队列的 REGISTRY 反向对账，而不是靠人记得住。
    """
    known = set(sampling.CREATIVE) | set(sampling.STRICT) | {"beats"}
    missing = set(tasks.REGISTRY) - known
    assert not missing, f"任务类型没配采样档位：{sorted(missing)}"
