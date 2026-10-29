"""文风画像：从作者自己的旧文里量出「他怎么写」，零模型调用。

为什么值得做：模板给的是「文体」，素材给的是「事实」，而「像不像我写的」取决于
句长、段长、人称、口头禅与标点习惯——这些全是可测的量，不该让模型每次猜。
统计口径写得尽量笨：切句、切段、数数、比值，不做词性分析。
数字算错了顶多是画像偏一点；用模型去「总结文风」反而会编出一堆形容词。

两个刻意的取舍：
1. 代码块不参与统计——样本里贴了代码，句长会被拉得没意义。
2. 只输出「有数据支撑」的结论：句长、人称、问号频率对得上才写，
   不写「文笔细腻」这种没法验证的话。
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Iterable

from app.studio import tokenize

# 句末：中文句号/问号/叹号/省略号，以及换行（样本里常见一段一句）
_SENT_END = re.compile(r"[。！？!?…]+[」』”\"']?|\n+")
# 段：空行分隔。公众号正文里就是空行分段
_PARA_SPLIT = re.compile(r"\n\s*\n")

# 口头禅候选：口语里高频、但停用词表不会滤掉的连接语
FILLERS: tuple[str, ...] = (
    "其实", "说白了", "换句话说", "说实话", "你会发现", "问题是", "所以说",
    "讲真", "老实说", "也就是说", "反过来", "至少", "举个例子", "我一直觉得",
    "你看", "关键是",
)

# 口径写在一处：改判定标准只改这里，画像文本跟着变
SHORT_SENT = 18
LONG_SENT = 40
SHORT_PARA = 60
MANY_QUESTIONS = 3.0
MANY_EXCLAMATIONS = 2.0


def split_sentences(text: str) -> list[str]:
    """切句。空白与纯标点不算句子。"""
    out: list[str] = []
    for part in _SENT_END.split(text or ""):
        part = part.strip()
        if part and any(not ch.isspace() for ch in part):
            out.append(part)
    return out


def split_paragraphs(text: str) -> list[str]:
    if not (text or "").strip():
        return []
    return [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]


def _visible(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _per_k(n: int, chars: int) -> float:
    return round(n * 1000.0 / chars, 1) if chars else 0.0


def analyze(texts: Iterable[str]) -> dict[str, Any]:
    """把若干篇旧文压成一组数字。返回空画像时 `chars == 0`。"""
    paras: list[str] = []
    for t in texts:
        paras.extend(split_paragraphs(t))
    # 代码块不算：它既不是句子也不是段落，混进来只会让句长统计失真
    paras = [p for p in paras if not p.lstrip().startswith("```")]

    body = "\n".join(paras)
    chars = _visible(body)
    sents: list[str] = []
    para_sent_counts: list[int] = []
    for p in paras:
        got = split_sentences(p)
        sents.extend(got)
        para_sent_counts.append(len(got))

    sent_lens = [_visible(s) for s in sents]
    sent_lens = [n for n in sent_lens if n > 0]
    para_lens = [_visible(p) for p in paras]
    para_lens = [n for n in para_lens if n > 0]

    if not chars or not sent_lens:
        return {"chars": chars, "sentences": 0, "paragraphs": len(para_lens)}

    stats: dict[str, Any] = {
        "chars": chars,
        "sentences": len(sent_lens),
        "paragraphs": len(para_lens),
        "sent_avg": round(statistics.fmean(sent_lens), 1),
        "sent_median": int(statistics.median(sent_lens)),
        "long_ratio": round(
            sum(1 for n in sent_lens if n > LONG_SENT) / len(sent_lens), 2
        ),
        "para_avg": round(statistics.fmean(para_lens), 1) if para_lens else 0.0,
        "para_max": max(para_lens) if para_lens else 0,
        "para_sents": round(statistics.fmean(para_sent_counts), 1)
        if para_sent_counts else 0.0,
        "you_per_k": _per_k(body.count("你"), chars),
        "i_per_k": _per_k(body.count("我"), chars),
        "q_per_k": _per_k(body.count("？") + body.count("?"), chars),
        "excl_per_k": _per_k(body.count("！") + body.count("!"), chars),
        "dash_per_k": _per_k(body.count("——"), chars),
        "ellipsis_per_k": _per_k(body.count("…"), chars),
    }

    counts = {w: body.count(w) for w in FILLERS}
    stats["fillers"] = [
        w for w, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 2
    ][:4]

    freq: dict[str, int] = {}
    for tok in tokenize.tokens(body):
        freq[tok] = freq.get(tok, 0) + 1
    stats["top_words"] = [
        w for w, n in sorted(freq.items(), key=lambda kv: -kv[1])
        if n >= 3 and len(w) >= 2
    ][:6]
    return stats


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def traits(stats: dict[str, Any]) -> list[str]:
    """从数字里能直接读出来的写法，最多三条。"""
    out: list[str] = []
    avg = float(stats.get("sent_avg") or 0)
    if avg and avg < SHORT_SENT:
        out.append("短句为主")
    elif avg > 28:
        out.append("句子偏长，常有从句套从句")
    if float(stats.get("q_per_k") or 0) >= MANY_QUESTIONS:
        out.append("爱用问句推进")
    you = float(stats.get("you_per_k") or 0)
    me = float(stats.get("i_per_k") or 0)
    if you > 3 and you >= me * 1.3:
        out.append("对着「你」说话")
    elif me > 3 and me > you:
        out.append("以「我」的视角讲")
    para = float(stats.get("para_avg") or 0)
    if para and para < SHORT_PARA:
        out.append("段落短，手机上不闷")
    if float(stats.get("excl_per_k") or 0) >= MANY_EXCLAMATIONS:
        out.append("敢用感叹号")
    return out[:3]


def profile_text(stats: dict[str, Any], *, samples: int = 0) -> str:
    """把数字写成一段能直接塞进 prompt 的画像。没有样本时返回空串。"""
    if not stats or not stats.get("chars") or not stats.get("sentences"):
        return ""
    chars = int(stats["chars"])
    head = f"文风画像（{samples} 篇旧文 / {chars} 字）" if samples else f"文风画像（{chars} 字）"
    # 0 的项不写：画像会整段进 S1，凑字数的「破折号 0.0 个」只是噪音
    long_ratio = float(stats.get("long_ratio") or 0)
    sent_line = (
        f"- 句子：平均 {stats['sent_avg']} 字，中位 {stats['sent_median']} 字"
        + (f"；超过 {LONG_SENT} 字的长句占 {_pct(long_ratio)}" if long_ratio >= 0.05 else "")
    )
    lines = [
        head,
        sent_line,
        f"- 段落：平均 {stats.get('para_sents')} 句 / {stats.get('para_avg')} 字，"
        f"最长 {stats.get('para_max')} 字",
    ]
    if stats.get("you_per_k") or stats.get("i_per_k"):
        who = "偏第二人称" if float(stats.get("you_per_k") or 0) >= float(
            stats.get("i_per_k") or 0) else "偏第一人称"
        lines.append(
            f"- 人称：{who}（「你」每千字 {stats.get('you_per_k')} 次，"
            f"「我」{stats.get('i_per_k')} 次）"
        )
    if stats.get("fillers"):
        lines.append("- 口头禅：" + "、".join(stats["fillers"]))
    marks: list[str] = []
    if float(stats.get("q_per_k") or 0) >= 1:
        marks.append(f"问号每千字 {stats['q_per_k']} 个")
    if float(stats.get("dash_per_k") or 0) >= 0.3:
        marks.append(f"破折号 {stats['dash_per_k']} 个")
    if float(stats.get("excl_per_k") or 0) >= 0.5:
        marks.append(f"感叹号 {stats['excl_per_k']} 个")
    if marks:
        lines.append("- 标点：" + "、".join(marks))
    if stats.get("top_words"):
        lines.append("- 常用词：" + "、".join(stats["top_words"]))
    got = traits(stats)
    if got:
        lines.append("- 手法：" + "、".join(got))
    return "\n".join(lines)


def build(texts: Iterable[str]) -> str:
    """一步到位：若干篇正文 → 画像文本。空输入返回空串。"""
    items = [t for t in texts if (t or "").strip()]
    if not items:
        return ""
    return profile_text(analyze(items), samples=len(items))