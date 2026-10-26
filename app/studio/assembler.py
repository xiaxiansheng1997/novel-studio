"""ContextAssembler v2：把「写什么、照什么写、写到哪了」按预算装进一次请求。

分桶：模板 S0 / 规范 S1 / 作品圣经 S2 / 前情 S3 / 最近摘要 S4 / 检索 S5
/ 图谱 S6 / 素材 S_src / 当前正文 S7 / 本轮指令 S8。

原则：每个桶先按自身上限截断，再拼装；截断必须留痕（warnings），
否则调 prompt 时永远不知道模型为什么"忘了"某段设定。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from app.studio import content_types, graph, retrieval

# 按形态给总预算（字符）——仅作为 YAML 缺失时的兜底
BUDGET = {"article": 18000, "novel": 28000}

# 以「写正文」为目标的任务：桶集合由内容类型决定，其余任务用固定集合
_DRAFT_TASKS = {"article_draft", "continue", "draft"}

# 每个任务启用哪些桶
TASK_BUCKETS = {
    "article_outline": ["S0", "S1", "S2", "S8"],
    "article_draft": ["S0", "S1", "S2", "S3", "S4", "S5", "S_src", "S7", "S8"],
    "continue": ["S0", "S1", "S2", "S3", "S4", "S5", "S_src", "S7", "S8"],
    "novel_outline": ["S1", "S2", "S8"],
    "beats": ["S1", "S2", "S3", "S4", "S6", "S8"],
    "draft": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"],
    "check": ["S1", "S2", "S4", "S6", "S7"],
    "polish": ["S0", "S1", "S7", "S8"],
    "summarize": ["S7"],
}

# 各桶的默认预算（字符）
BUCKET_BUDGET = {
    "S0": 1500,
    "S1": 1200,
    "S2": 4000,
    "S3": 2000,
    "S4": 3000,
    "S5": 6000,
    "S6": 2500,
    "S_src": 4500,
    "S7": 8000,
    "S8": 800,
}

BUCKET_LABEL = {
    "S0": "结构模板",
    "S1": "写作规范",
    "S2": "作品圣经",
    "S3": "前情概要",
    "S4": "最近节摘要",
    "S5": "检索证据",
    "S6": "图谱子图",
    "S_src": "素材证据",
    "S7": "当前正文",
    "S8": "本轮指令",
}


@dataclass
class Bucket:
    id: str
    label: str
    budget: int
    text: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "budget": self.budget, "chars": self.chars}


@dataclass
class AssemblyResult:
    system: str
    user: str
    buckets: list[Bucket] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return len(self.system) + len(self.user)

    def as_dict(self) -> dict[str, Any]:
        return {
            "buckets": [b.as_dict() for b in self.buckets],
            "total_chars": self.total_chars,
            "warnings": self.warnings,
            "system": self.system,
            "user": self.user,
        }


def _head(text: str, limit: int) -> tuple[str, bool]:
    """按段落边界截头部，返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    para = cut.rfind("\n")
    if para > limit * 0.6:
        cut = cut[:para]
    return cut, True


def _tail(text: str, limit: int) -> tuple[str, bool]:
    """截尾部（续写要用最近的上下文），尽量从句号后开始。"""
    if len(text) <= limit:
        return text, False
    cut = text[-limit:]
    para = cut.find("\n")
    if 0 <= para < limit * 0.4:
        cut = cut[para + 1:]
    return cut, True


def _fmt_outline(outline_json: str) -> str:
    try:
        items = json.loads(outline_json or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(items, list):
        return ""
    lines = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        head = it.get("heading", "")
        prompt = it.get("prompt", "")
        words = it.get("target_words")
        suffix = f"（约 {words} 字）" if words else ""
        lines.append(f"{i}. {head}{suffix}：{prompt}")
    return "\n".join(lines)


def _style_text(work: dict) -> str:
    raw = work.get("style_json") or "{}"
    try:
        style = json.loads(raw)
    except json.JSONDecodeError:
        style = {}
    if not isinstance(style, dict):
        return ""
    parts = []
    for key, label in (
        ("person", "人称"),
        ("tense", "时态"),
        ("tone", "语气"),
        ("taboo", "禁忌"),
    ):
        val = style.get(key)
        if val:
            parts.append(f"- {label}：{val}")
    return "\n".join(parts)


async def assemble(
    store,
    work: dict,
    node: Optional[dict],
    task_type: str,
    instruction: str = "",
    *,
    template: Optional[dict] = None,
    resolved: Optional[dict] = None,
) -> AssemblyResult:
    """装配一次生成的上下文。

    `template` 由调用方查好后传入（避免装配器自己再查一次库）；
    为空时按 work.template_id 回退。
    `resolved` 是 content_types.resolve() 的结果；不传则内部现算，
    保证直接调 assemble 的调用方（脚本/测试）行为一致。
    """
    kind = work.get("kind") or "article"
    scale = work.get("scale") or "single"
    subtype = work.get("subtype") or ""
    if resolved is None:
        resolved = content_types.resolve(kind, scale, subtype)

    if task_type in _DRAFT_TASKS:
        enabled = list(resolved.get("buckets") or [])
    else:
        enabled = list(TASK_BUCKETS.get(task_type) or [])
    enabled = [b for b in enabled if b in BUCKET_LABEL] or list(TASK_BUCKETS["article_draft"])

    total_budget = int(resolved.get("budget_chars") or BUDGET.get(kind, 18000))

    buckets: dict[str, Bucket] = {bid: Bucket(bid, BUCKET_LABEL[bid], BUCKET_BUDGET[bid]) for bid in enabled}
    warnings: list[str] = []

    # ---------- S0 结构模板 ----------
    if "S0" in buckets:
        parts = []
        if template:
            parts.append(f"文体：{template.get('name', '')}")
            outline = _fmt_outline(template.get("outline_json", ""))
            if outline:
                parts.append("骨架：\n" + outline)
            if template.get("style_prompt"):
                parts.append("文体要求：" + str(template["style_prompt"]))
        text = "\n".join(parts)
        if text:
            buckets["S0"].text = text
        else:
            buckets.pop("S0", None)

    # ---------- S1 写作规范 ----------
    if "S1" in buckets:
        style = _style_text(work)
        base = "你是专业的中文写作助手。只输出正文内容，不要解释你的写作过程。"
        rules = [
            "- 严格遵循下方给出的设定与素材，不引入未提供的事实",
            "- 不要使用「首先/其次/最后」「随着…的发展」这类套话（除非文体要求）",
        ]
        if kind == "novel":
            rules.append("- 多写动作与细节，少写独白；每章结尾留钩子")
        type_style = resolved.get("style_prompt") or ""
        type_rules = resolved.get("instructions") or ""
        text = base + "\n" + "\n".join(rules)
        if type_style:
            text += "\n" + type_style
        if type_rules:
            text += "\n" + type_rules
        if style:
            text += "\n" + style
        buckets["S1"].text, cut = _head(text, buckets["S1"].budget)
        if cut:
            warnings.append("S1 写作规范超出预算，已截断")

    # ---------- S2 作品圣经 ----------
    if "S2" in buckets:
        lines = []
        title = work.get("title") or "未命名"
        premise = work.get("premise") or ""
        lines.append(f"作品：{title}")
        if resolved.get("label"):
            bits = [str(resolved.get("label"))]
            if resolved.get("subtype_label"):
                bits.append(str(resolved["subtype_label"]))
            lines.append("类型：" + " / ".join(bits))
        if premise:
            lines.append(f"主旨：{premise}")
        if work.get("target_words"):
            lines.append(f"目标字数：{work['target_words']}")
        # 已写节点的标题即天然大纲；带 ordinal 的才是可读单元
        tree = await store.list_tree(work["id"])
        readable = [n for n in tree if n.get("ordinal") is not None]
        if readable:
            readable.sort(key=lambda n: n.get("ordinal") or 0)
            outline_lines = [
                f"{n['ordinal']}. {n['title']}（{n.get('word_count') or 0} 字，{n.get('status')}）"
                for n in readable
            ]
            lines.append("已完成的节点：\n" + "\n".join(outline_lines))
        text = "\n".join(lines)
        buckets["S2"].text, cut = _head(text, buckets["S2"].budget)
        if cut:
            warnings.append("S2 作品圣经超出预算，已按顺序截断（靠后的节点未列出）")

    # ---------- S3 前情概要（父容器/全书摘要） ----------
    # 过期的一律不注入：少一份记忆只是少点背景，但旧的概要是假的，
    # 模型会把已经改了的情节当成既定事实写下去。
    if "S3" in buckets:
        text = ""
        stale_bits: list[str] = []
        parent_id = (node or {}).get("parent_id")
        if parent_id:
            sm = await store.get_summary("container", parent_id)
            if sm and sm.get("content"):
                if sm.get("fresh"):
                    text = "本卷/本讲概要：\n" + sm["content"]
                else:
                    stale_bits.append("本卷")
        if not text:
            wsm = await store.get_summary("work", work["id"])
            if wsm and wsm.get("content"):
                if wsm.get("fresh"):
                    text = "全书概要：\n" + wsm["content"]
                else:
                    stale_bits.append("全书")
        if text:
            buckets["S3"].text, cut = _head(text, buckets["S3"].budget)
            if cut:
                warnings.append("S3 前情概要超出预算，已截断")
        else:
            buckets.pop("S3", None)
        if stale_bits:
            warnings.append(
                "S3 里" + "/".join(stale_bits) + "的摘要已过期（正文改过还没重算），本轮未注入："
                "在右栏「上下文」点「重算过期摘要」补回"
            )

    # ---------- S4 最近节摘要（三段式记忆卡） ----------
    if "S4" in buckets:
        current = int((node or {}).get("ordinal") or 0) or None
        rows = await store.list_summaries(work["id"], "node")
        rows = [
            r for r in rows
            if r.get("content") and (current is None or int(r.get("ref_ordinal") or 0) < current)
        ]
        # 先挑过期的报出去，再取尾部 6 条：不能让一节旧摘要占着名额
        stale = [int(r.get("ref_ordinal") or 0) for r in rows if not r.get("fresh")]
        rows = [r for r in rows if r.get("fresh")][-6:]
        if rows:
            tree_map = {n["id"]: n for n in await store.list_tree(work["id"])}
            parts = []
            for r in rows:
                title = (tree_map.get(r["ref_id"]) or {}).get("title") or "未命名"
                parts.append(f"第 {r.get('ref_ordinal')} 节《{title}》：\n{r['content']}")
            # 取尾部：最近几节比最早几节重要
            buckets["S4"].text, cut = _tail("\n\n".join(parts), buckets["S4"].budget)
            if cut:
                warnings.append(f"S4 只保留了最近 {len(rows)} 节摘要的尾部")
        else:
            buckets.pop("S4", None)
        if stale:
            shown = "、".join(str(x) for x in stale[:8])
            more = f" 等 {len(stale)} 节" if len(stale) > 8 else ""
            warnings.append(
                f"第 {shown}{more} 的摘要已过期（正文改过还没重算），本轮未注入"
            )

    # ---------- S6 图谱子图（仅小说） ----------
    # 放在检索之前：先给「谁是谁、什么关系、谁死了」，再给原文片段。
    # 顺序反了模型会先被片段带跑，把旧设定当成新事实。
    if "S6" in buckets:
        if kind != "novel":
            buckets.pop("S6", None)
        else:
            current_ordinal = int((node or {}).get("ordinal") or 0)
            probe = " ".join(x for x in (
                (node or {}).get("title"), instruction, work.get("premise")) if x)
            sg = await graph.subgraph(
                store, work["id"], text=probe, hops=2, limit=40)
            text = graph.format_subgraph(
                sg, budget=buckets["S6"].budget, current_ordinal=current_ordinal)
            if text:
                buckets["S6"].text = text
            else:
                buckets.pop("S6", None)
                warnings.append(
                    "图谱为空：这本小说还没抽过设定，S6 无内容。"
                    "先调一次 /studio/works/{id}/graph/extract 再写正文"
                )

    # ---------- S_src 素材证据 ----------
    if "S_src" in buckets:
        sources = await store.list_sources(work["id"], limit=5)
        if not sources:
            buckets.pop("S_src", None)
            if resolved.get("requires_sources"):
                warnings.append(
                    f"该类型（{resolved.get('subtype_label') or kind}）要求有素材，当前一条都没有："
                    "模型只能靠已有知识写，事实错误风险高"
                )
            elif kind == "article":
                warnings.append("该作品还没有素材，生成质量会明显下降（建议先导入资料）")
        else:
            chunks = []
            remain = buckets["S_src"].budget
            for s in sources:
                full = await store.get_source(s["id"])
                if not full:
                    continue
                body, cut = _head(full.get("content") or "", min(1500, max(200, remain)))
                if not body:
                    continue
                head_bits = [f"《{s.get('title') or '未命名素材'}》"]
                if s.get("published_at"):
                    head_bits.append(f"（{s['published_at']}）")
                if s.get("url"):
                    head_bits.append(f" {s['url']}")
                chunks.append("".join(head_bits) + "\n" + body)
                remain -= len(body)
                if remain <= 300:
                    break
            if chunks:
                buckets["S_src"].text = "\n\n---\n\n".join(chunks)
                if len(sources) > len(chunks):
                    warnings.append(f"S_src 预算只够 {len(chunks)}/{len(sources)} 条素材")

    # ---------- S7 当前正文 ----------
    if "S7" in buckets and node:
        content = node.get("content") or ""
        if content:
            tail, cut = _tail(content, buckets["S7"].budget)
            if cut:
                warnings.append(f"S7 正文超出预算，只保留最近 {len(tail)} 字")
            buckets["S7"].text = tail
        else:
            buckets.pop("S7", None)

    # ---------- S8 指令 ----------
    if "S8" in buckets:
        buckets["S8"].text, _ = _head(instruction or "", buckets["S8"].budget)

    # ---------- S5 检索证据 ----------
    # 预算动态计算：其它桶先分，剩下的才是证据的。这样加检索不会凭空超总预算。
    if "S5" in buckets and node:
        used = sum(b.chars for bid, b in buckets.items() if bid != "S5")
        room = max(1200, min(buckets["S5"].budget, total_budget - used))
        query = retrieval.build_query(node.get("title"), instruction, work.get("premise"))
        hits = await retrieval.retrieve(
            store,
            work["id"],
            query=query,
            current_ordinal=int(node.get("ordinal") or 0) or None,
            kinds=("body",),
            top_k=8,
            budget_chars=room,
            exclude_node_ids={node["id"]},
        )
        if hits:
            buckets["S5"].budget = room
            buckets["S5"].text = retrieval.format_evidence(hits)
            if used + buckets["S5"].chars > total_budget:
                warnings.append(
                    f"装配总量 {used + buckets['S5'].chars} 超出预算 {total_budget}，S5 已尽量压缩"
                )
        else:
            buckets.pop("S5", None)

    # ---------- 拼装 ----------
    system = buckets["S1"].text if "S1" in buckets else ""
    tags = {
        "S0": ("template", "结构模板"),
        "S2": ("work_brief", "作品设定与已完成节点"),
        "S3": ("container_summary", "前情概要：上一层的整体进展"),
        "S4": ("recent_summaries", "最近几节发生了什么（事件/变化/钩子）"),
        "S6": ("graph", "已知设定与关系（已发生的事实，不得覆盖或反向）"),
        "S5": ("retrieved", "相关既往正文（可引用，但不要编造未出现的信息）"),
        "S_src": ("sources", "参考素材（只能依据这些内容，不要编造）"),
        "S7": ("current_draft", "当前已写正文"),
        "S8": ("instruction", "本轮任务"),
    }
    parts = []
    for bid, (tag, note) in tags.items():
        b = buckets.get(bid)
        if b and b.text:
            parts.append(f"<{tag}>\n<!-- {note} -->\n{b.text}\n</{tag}>")
    user = "\n\n".join(parts)

    ordered = [
        buckets[bid]
        for bid in ("S0", "S1", "S2", "S3", "S4", "S6", "S5", "S_src", "S7", "S8")
        if bid in buckets
    ]
    result = AssemblyResult(system=system, user=user, buckets=ordered, warnings=warnings)

    if result.total_chars > total_budget:
        warnings.append(
            f"装配总量 {result.total_chars} 超出 {kind} 预算 {total_budget}，"
            "请调小某个桶的预算或减少素材"
        )
    return result
