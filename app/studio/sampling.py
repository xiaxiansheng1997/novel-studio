"""采样档位：不同任务要的不是同一种「创造力」。

为什么要有这张表：以前所有调用都不带 temperature，等于把写正文和抽 JSON 一起
交给上游默认值。官方 API 的默认温度普遍偏高（1.0 上下），拿去拆结构就是让
模型在「信息差该填什么」这种硬字段上自由发挥；反过来本地小模型的默认值常把
正文写成平铺直叙的流水账。参数只有直连通道用得上——反代网页端没有这些开关，
传了也是忽略，所以这里给的是「建议档位」，不会因为有通道不支持而报错。

为什么放在 studio 而不是 gateway：gateway 是传输层，它只该接受显式数值；
「draft 要比 summarize 更发散」是写作域的判断，别把任务类型这种词汇漏下去。
"""
from __future__ import annotations

from typing import Any

# 正文创作：要发散。低于 0.6 左右小说会写成说明书
CREATIVE = ("draft", "article_draft", "polish")
# 结构化产出：要收敛。JSON 合法性和事实准确比文采重要
STRICT = ("summarize", "extract", "check", "craft")
# 改写：换说法要一点自由度，但情节一点都不该动
REWRITE = ("deai",)

# 创作档：0.9 是「有变化但不失控」的经验值。想更稳就在页面把模型换成
# 反代的 think 模式，或在这里调低——这里是唯一一处，改完全部任务跟着变
TEMP_CREATIVE = 0.9
# 节拍卡是「定这几件事发生」，不是写文采，比正文低一档
TEMP_BEATS = 0.7
# 严格档：留一点改写余地（0.0 会让部分端点直接拒绝请求）
TEMP_STRICT = 0.2
# 改写档：去 AI 味要真换说法（0.2 时模型会原句照抄），但不能改事（
# 0.9 时它会顺手把情节也重写）。0.5 是两边都拧得动的位置。
TEMP_REWRITE = 0.5


def for_task(task_type: str) -> dict[str, Any]:
    """返回要透传给 gateway.stream/complete 的参数（键名与其入参一致）。

    未知任务类型返回空 dict：宁可沿用上游默认，也不要替调用方瞎猜一个温度。
    """
    if task_type in STRICT:
        out: dict[str, Any] = {"temperature": TEMP_STRICT}
        if task_type == "craft":
            # 拆解的输出契约本来就是 JSON：直连通道交给上游保证合法，比我们在
            # 提示词里恳求「务必只输出 JSON」可靠得多（本地 14B 尤其吃这一条）
            out["json_mode"] = True
        return out
    if task_type in CREATIVE:
        return {"temperature": TEMP_CREATIVE}
    if task_type in REWRITE:
        return {"temperature": TEMP_REWRITE}
    if task_type == "beats":
        return {"temperature": TEMP_BEATS}
    return {}
