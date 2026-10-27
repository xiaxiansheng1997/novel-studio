"""写作指令拼装：把「节点 + 大纲要点 + 用户附加要求」压成一段具体指令。

单独抽出来是因为两条路径都要用：
- 交互式生成（`/studio/nodes/{id}/generate`）
- 队列任务（`app/studio/tasks.py` 的 draft 执行器）

两处各写一份必然漂移，而字数约束是逐字调出来的（实测从 337 字提到 1075 字），
漂了就白调。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.studio import jsonutil


def work_meta(work: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(work.get("meta_json") or "{}")
        return meta if isinstance(meta, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def node_instruction(node: dict[str, Any], meta: dict[str, Any], extra: str = "") -> str:
    """本节的具体写作指令（大纲里的 prompt + 用户附加要求 + 篇幅硬约束）。"""
    info = (meta.get("outline") or {}).get(node["id"]) or {}
    bits = [f"本节标题：{node['title']}"]
    if info.get("prompt"):
        bits.append(f"本节要点：{info['prompt']}")
    # 节拍卡（novel 的 beats 任务产出）优先于大纲要点：它是更近的一次规划
    if (node.get("summary") or "").strip():
        bits.append("本章节拍（必须按这个顺序推进）：\n" + node["summary"].strip())
    target = int(info.get("target_words") or 0)
    if target:
        low = int(target * 0.8)
        bits.append(
            f"篇幅：约 {target} 字，不足 {low} 字视为不合格。"
            "宁可多写具体细节、数据与例子，也不要提前收尾。"
        )
    if extra:
        bits.append(f"附加要求：{extra}")
    bits.append("只输出本节正文，不要重复标题，不要写下一节的过渡句。")
    return "\n".join(bits)


def target_words_of(node: dict[str, Any], meta: dict[str, Any], resolved: dict[str, Any]) -> int:
    """本节的目标字数：优先大纲，其次按类型目标均摊，最后用 1000 兜底。"""
    info = (meta.get("outline") or {}).get(node["id"]) or {}
    try:
        got = int(info.get("target_words") or 0)
    except (TypeError, ValueError):
        got = 0
    if got:
        return got
    total = int(resolved.get("target_words") or 0)
    if total:
        layers = resolved.get("layers") or []
        # 有容器层（讲/卷）时，目标字数分到可读层
        return max(600, total // 20) if layers and resolved.get("container_kind") else max(600, total // 8)
    return 1000


def beats_prompt(work: dict[str, Any], node: dict[str, Any], *, prev_hint: str = "",
                 focus: str = "", known_names: Optional[list[str]] = None) -> tuple[str, str]:
    """章纲（节拍卡）：先定「发生哪几件事」再写正文。

    这是整条小说流水线里性价比最高的一步：跳过它直接批量写，
    产出的东西基本不可用（事件靠模型自由发挥，前后必然打架）。

    `focus` 是大纲里本章的要点，`known_names` 是已抽出的人物/地点。
    两个都要给：不给 focus，模型不知道这章要推进什么；不给名字，
    它会新造一套人名（实测出现过「沈砚/陆五」而正文里是「林昭/周叙」）。
    """
    names = [n for n in (known_names or []) if n and len(n) >= 2][:40]
    system = (
        "你是中文小说的策划编辑。你只输出本章节拍卡，不要写正文、不要解释。"
        "节拍卡要让作者一眼看出这一章推进了什么，不能是空话。"
        "人物必须用给定的名字，禁止新造人名。"
    )
    blocks = [
        f"作品：{work['title']}",
        f"主旨：{work.get('premise') or '（未填）'}",
        f"本章：第 {node.get('ordinal') or 0} 章《{node['title']}》",
    ]
    if focus:
        blocks.append(f"本章要点：{focus}")
    if names:
        blocks.append("已有人物/地点（必须复用这些名字，不能新造）：\n"
                      + "\n".join(f"- {n}" for n in names))
    if prev_hint:
        blocks.append(f"上一章发生了什么：{prev_hint}")

    blocks.append(
        "\n请给出本章节拍卡，格式严格如下（4-6 行，每行一句）：\n"
        "出场：谁在这一章出现（从上面名单里选，不要新造人）\n"
        "地点：发生在哪（同上）\n"
        "事件：\n- 第一件事\n- 第二件事\n- 第三件事\n"
        "变化：这一章结束后，谁的处境/关系/掌握的信息变了\n"
        "钩子：结尾留下什么悬念\n"
    )
    return system, "\n".join(blocks)


def parse_beats(text: str) -> str:
    """把节拍卡清洗成可直接进 instruction 的文本。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return "\n".join(lines)[:800]


def facts_prompt(source: dict[str, Any], *, limit: int = 5) -> tuple[str, str]:
    """从一条素材里抽事实卡：只要「能对原文核对的一句话」，不要观点与修辞。

    事实卡是给一致性检查与生成时「优先使用」的那一层。原来从素材到事实卡
    只能靠人手工抄，抄不动就等于这条链路不存在。
    """
    system = (
        "你是资料整理助手。你只输出 JSON 数组，不要解释、不要代码块、不要前后缀。"
        "每条事实必须是原文里能直接核对的一句话，不能推测、不能改写成观点。"
    )
    body = (source.get("content") or "")[:12000]
    blocks = [
        f"素材标题：{source.get('title') or '未命名素材'}",
        f"资料日期：{source.get('published_at') or '未知'}",
        f"原文：\n{body}",
        "",
        f"请抽出最多 {limit} 条事实，严格输出 JSON 数组，格式：",
        '[{"statement": "一句可引用的话（不超过 60 字）", "valid_until": "YYYY-MM 或空串"}]',
        "要求：数字、时间、人名、机构名必须与原文一致；原文没有的不要补；"
        "「应该 / 值得 / 最好」这类主张不要。",
    ]
    return system, "\n".join(blocks)


def parse_facts(text: str) -> list[dict[str, str]]:
    """把模型输出归一成 [{statement, valid_until}]：去重、丢掉过短的、最多 10 条。"""
    items = jsonutil.as_list(jsonutil.parse_json_loose(text))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for it in items:
        if isinstance(it, dict):
            statement = jsonutil.as_str(it.get("statement") or it.get("fact"), 200)
            valid = jsonutil.as_str(it.get("valid_until"), 20)
        else:
            statement = jsonutil.as_str(it, 200)
            valid = ""
        if len(statement) < 6 or statement in seen:
            continue
        seen.add(statement)
        out.append({"statement": statement, "valid_until": valid})
        if len(out) >= 10:
            break
    return out
