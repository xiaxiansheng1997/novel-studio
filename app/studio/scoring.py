"""质量打分：每节生成完立刻算一个 0-100 分，零 LLM 成本。

为什么必须自动打分：

全自动批量最大的风险不是「写得慢」，而是「连续跑 50 章全是废稿」。
等人工从头读到尾才发现，代价是几小时和一次风控。所以每条产出都要有个
可比的数字，让人只需看分数最低的那几条。

五个维度（权重写死在表里，改动要同步改这里的注释与文档）：

| 维度 | 权重 | 算法 |
|---|---|---|
| 字数达标 | 25% | 实际/目标，偏离 ±40% 开始扣 |
| 结构完整 | 25% | 非空 + 段落数 + 结尾是否收住 |
| 设定合规 | 20% | 硬规则告警数（error 扣得比 warn 重） |
| 重复率 | 20% | 与近 5 节的字符 5-gram 重叠率 |
| 依据强度 | 10% | 小说看引用了多少已知实体；文档看有数字却无来源的段落占比 |

得分映射：≥80 可直接用，60-79 小改，<60 建议重写。分数写进 `runs.score`。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# 各维度权重，合计 1.0
WEIGHTS = {"length": 0.25, "structure": 0.25, "compliance": 0.20,
           "novelty": 0.20, "grounding": 0.10}

GRADE_OK = 80
GRADE_MINOR = 60

# 5-gram 重叠超过这个值开始扣分：中文小说里常见套话会自然重合 10% 左右
DUP_SOFT = 0.25
DUP_HARD = 0.55

CITE_RE = re.compile(r"(据[^，。]{0,12}(?:报道|统计|记载)|来源[:：]|参考(?:资料|文献)|引自"
                     r"|《[^》]{2,20}》|数据显示|官方通报)")
NUM_RE = re.compile(r"\d{3,}|(?:19|20)\d{2}\s*年")


def _grams(text: str, n: int = 5) -> set[str]:
    clean = re.sub(r"\s+", "", text or "")
    if len(clean) < n:
        return set()
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


def overlap_ratio(text: str, others: list[str], *, n: int = 5) -> float:
    """与若干段旧文本的最大 5-gram 重叠率。取最大而不是平均：
    抄了上一章一整段，平均下来可能不明显，但最大值一定是高的。"""
    mine = _grams(text, n)
    if not mine:
        return 0.0
    worst = 0.0
    for other in others:
        theirs = _grams(other, n)
        if not theirs:
            continue
        worst = max(worst, len(mine & theirs) / len(mine))
    return worst


def _score_length(actual: int, target: int) -> tuple[float, str]:
    if not target:
        return 1.0, ""
    ratio = actual / target
    if 0.6 <= ratio <= 1.6:
        return 1.0, ""
    if ratio < 0.6:
        score = max(0.0, ratio / 0.6)
        return score, f"字数 {actual}/{target}，只有目标的 {ratio:.0%}"
    score = max(0.0, 1.0 - (ratio - 1.6))
    return score, f"字数 {actual}/{target}，超出目标的 {ratio:.0%}"


def _score_structure(content: str) -> tuple[float, str]:
    text = (content or "").strip()
    if not text:
        return 0.0, "正文为空"
    paras = [p for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    score = 0.4
    if len(paras) >= 3:
        score += 0.4
    elif len(paras) == 2:
        score += 0.2
    # 结尾没有句末标点通常意味着被截断
    if text[-1] in "。！？」』…\"'":
        score += 0.2
    note = "" if score >= 0.99 else f"只有 {len(paras)} 段" + ("" if text[-1] in "。！？」』…" else "，结尾疑似截断")
    return min(1.0, score), note


def _score_compliance(issues: list[dict]) -> tuple[float, str]:
    errors = sum(1 for i in issues if (i.get("severity") or "") == "error")
    warns = sum(1 for i in issues if (i.get("severity") or "") == "warn")
    if not errors and not warns:
        return 1.0, ""
    penalty = min(1.0, errors * 0.45 + warns * 0.15)
    return max(0.0, 1.0 - penalty), f"告警 {errors} 错误 / {warns} 警告"


def _score_novelty(content: str, recent: list[str]) -> tuple[float, str]:
    if not recent:
        return 1.0, ""
    ratio = overlap_ratio(content, recent)
    if ratio <= DUP_SOFT:
        return 1.0, ""
    if ratio >= DUP_HARD:
        return 0.0, f"与近 {len(recent)} 节重复度 {ratio:.0%}"
    span = (ratio - DUP_SOFT) / (DUP_HARD - DUP_SOFT)
    return max(0.0, 1.0 - span), f"与近 {len(recent)} 节重复度 {ratio:.0%}"


def _score_grounding(content: str, *, kind: str,
                     entity_names: list[str]) -> tuple[float, str]:
    if kind == "novel":
        if not entity_names:
            return 1.0, ""  # 还没建图，不该惩罚正文
        hits = sum(1 for name in entity_names if len(name) >= 2 and name in content)
        # 一章里出现 2 个以上已知实体就算接上了前文
        if hits >= 2:
            return 1.0, ""
        return (0.5, f"只提到 {hits} 个已知人物/地点") if hits else (0.2, "没有提到任何已知人物/地点")

    # 文档：有数字却没标注来源的段落占比
    paras = [p for p in re.split(r"\n\s*\n|\n", content or "") if p.strip()]
    if not paras:
        return 0.0, "正文为空"
    bare = [p for p in paras if NUM_RE.search(p) and not CITE_RE.search(p)]
    if not bare:
        return 1.0, ""
    ratio = len(bare) / len(paras)
    return max(0.0, 1.0 - ratio * 1.5), f"{len(bare)}/{len(paras)} 段有数字无来源"


def grade_of(score: float) -> str:
    if score >= GRADE_OK:
        return "ok"
    if score >= GRADE_MINOR:
        return "minor"
    return "rewrite"


def score_node(
    node: dict[str, Any],
    content: str,
    *,
    kind: str = "article",
    target_words: int = 0,
    issues: Optional[list[dict]] = None,
    recent_texts: Optional[list[str]] = None,
    entity_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    """给一节正文打分。返回的 parts 里每个维度都有 0-1 的分数，便于前端画条形图。"""
    parts: dict[str, float] = {}
    notes: list[str] = []

    score, note = _score_length(len((content or "").strip()), int(target_words or 0))
    parts["length"] = round(score, 3)
    if note:
        notes.append(note)

    score, note = _score_structure(content)
    parts["structure"] = round(score, 3)
    if note:
        notes.append(note)

    score, note = _score_compliance(issues or [])
    parts["compliance"] = round(score, 3)
    if note:
        notes.append(note)

    score, note = _score_novelty(content, recent_texts or [])
    parts["novelty"] = round(score, 3)
    if note:
        notes.append(note)

    score, note = _score_grounding(content, kind=kind, entity_names=entity_names or [])
    parts["grounding"] = round(score, 3)
    if note:
        notes.append(note)

    total = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)
    score100 = round(total * 100, 1)
    return {
        "score": score100,
        "grade": grade_of(score100),
        "parts": parts,
        "notes": notes,
    }


def recent_texts_of(store_texts: list[str], k: int = 5) -> list[str]:
    """取最近 k 段正文用于重复度比对。"""
    return [t for t in store_texts[-k:] if t]


def parse_outline_target(meta_json: str, node_id: str) -> int:
    """从 works.meta_json.outline 里取本节目标字数。"""
    try:
        meta = json.loads(meta_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return 0
    try:
        return int(((meta.get("outline") or {}).get(node_id) or {}).get("target_words") or 0)
    except (TypeError, ValueError):
        return 0
