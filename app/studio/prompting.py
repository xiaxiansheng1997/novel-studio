"""写作指令拼装：把「节点 + 大纲要点 + 用户附加要求」压成一段具体指令。

单独抽出来是因为两条路径都要用：
- 交互式生成（`/studio/nodes/{id}/generate`）
- 队列任务（`app/studio/tasks.py` 的 draft 执行器）

两处各写一份必然漂移，而字数约束是逐字调出来的（实测从 337 字提到 1075 字），
漂了就白调。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.studio import jsonutil, tokenize


def work_meta(work: dict[str, Any]) -> dict[str, Any]:
    try:
        meta = json.loads(work.get("meta_json") or "{}")
        return meta if isinstance(meta, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def node_plan(node: dict[str, Any]) -> dict[str, Any]:
    """读节点自己的计划（`nodes.plan_json`）。

    计划是作者写的，坏 JSON 不该让整次生成失败：读不出来就当没有计划，
    下游会退回 `works.meta_json.outline`。
    """
    raw = node.get("plan_json")
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    out: dict[str, Any] = {}
    for key in ("prompt", "pov", "time", "place"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    try:
        out["target_words"] = int(data.get("target_words") or 0)
    except (TypeError, ValueError):
        out["target_words"] = 0
    return out


def node_outline(node: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """本节计划：节点上的新计划优先，旧的大纲 meta 只做兜底。

    升级前每节的 prompt / target_words 存在 `works.meta_json.outline` 里；
    现在作者能在右栏直接改，改的是 `nodes.plan_json`。两条都要能读到，
    否则老书打开「计划」页会是空的。
    """
    plan = node_plan(node)
    legacy = (meta.get("outline") or {}).get(node["id"]) or {}
    if not isinstance(legacy, dict):
        legacy = {}
    prompt = plan.get("prompt") or str(legacy.get("prompt") or "").strip()
    try:
        legacy_target = int(legacy.get("target_words") or 0)
    except (TypeError, ValueError):
        legacy_target = 0
    return {
        "prompt": prompt,
        "target_words": plan.get("target_words") or legacy_target,
        "pov": plan.get("pov", ""),
        "time": plan.get("time", ""),
        "place": plan.get("place", ""),
    }


def node_instruction(node: dict[str, Any], meta: dict[str, Any], extra: str = "") -> str:
    """本节的具体写作指令（大纲里的 prompt + 用户附加要求 + 篇幅硬约束）。"""
    info = node_outline(node, meta)
    bits = [f"本节标题：{node['title']}"]
    if info.get("prompt"):
        bits.append(f"本节要点：{info['prompt']}")
    if info.get("pov"):
        bits.append(f"叙事视角：{info['pov']}")
    if info.get("time"):
        bits.append(f"故事内时间：{info['time']}")
    if info.get("place"):
        bits.append(f"地点：{info['place']}")
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


# 标题里的卷/章序号不是检索线索：「第一章 夜访」里的「第一章」全库都有。
# 分词前先在原文里抠一遍：jieba 会把「第一章」切成「第一」「章」，
# 事后按 token 过滤只能拦住后者。
_ORDINAL_IN_TEXT = re.compile(r"第\s*[0-9一二三四五六七八九十百千零两]{1,4}\s*[章节卷回部篇讲]")
_ORDINAL = re.compile(r"^第[0-9一二三四五六七八九十百千零两]{1,4}[章节卷回部篇讲]$")


def retrieval_terms(
    node: dict[str, Any], meta: dict[str, Any], *, extra: str = ""
) -> list[str]:
    """本节检索用的关键词，按重要度排序。

    不能把 `node_instruction()` 整段拿去分词：它一半篇幅是模板句
    （「本节标题」「篇幅：约 1000 字」「只输出本节正文」）。实测一节 123 个词，
    `match_query` 只取前 12 个，其中一半是套话，而计划里真正的地名/器物
    （「西市」「暗格」「令底」）全被挡在门外——检索不是没召回，是根本没问出去。

    取词顺序就是重要度：标题 → 地点/时间 → 要点 → 节拍卡 → 附加要求。
    “地点/时间”排在“要点”前面：它们是作者点名的最硬线索，而要点常是长句。
    """
    node = node or {}
    info = node_outline(node, meta)
    raw: list[str] = []
    if node.get("title"):
        raw.append(_ORDINAL_IN_TEXT.sub(" ", str(node["title"])))
    for key in ("place", "time"):
        if info.get(key):
            raw.append(str(info[key]))
    if info.get("prompt"):
        raw.append(str(info["prompt"]))
    beat = str(node.get("summary") or "").strip()
    if beat:
        raw.append(beat)
    if extra:
        raw.append(str(extra))

    out: list[str] = []
    for tok in tokenize.tokens(" ".join(raw)):
        if tok in out or tok.isdigit() or _ORDINAL.match(tok):
            continue
        out.append(tok)
    return out


def retrieval_phrases(node: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    """检索里当「短语」用的短串：去掉序号后的标题 + 地点。

    长句在 FTS 里不可能连续命中，但「西市仓库」这种 3-4 字的地名一旦在正文里
    原文出现，就是比散词命中硬得多的证据。`retrieve` 会对候选块做子串匹配并加成。
    """
    node = node or {}
    info = node_outline(node, meta)
    out: list[str] = []
    for raw in (
        _ORDINAL_IN_TEXT.sub(" ", str(node.get("title") or "")),
        str(info.get("place") or ""),
    ):
        p = raw.strip().strip("《》〈〉\"'“” ")
        # 短于 3 字（「夜访」）太容易误中；带空白说明不是一个连续串
        if len(p) >= 3 and p not in out and " " not in p:
            out.append(p)
    return out


def target_words_of(node: dict[str, Any], meta: dict[str, Any], resolved: dict[str, Any]) -> int:
    """本节的目标字数：优先节点计划，其次旧大纲，再按类型目标均摊，最后用 1000 兜底。"""
    got = int(node_outline(node, meta).get("target_words") or 0)
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


# ==================== 去 AI 味改写（tasks.run_deai） ====================

# 三条硬要求是针对三种最常见的坏结果：改情节、改对白的意思、把一段拆成三段。
# 拆段最隐蔽——贴回时找不到原段，整节的段落编号会错位，
# 而「逐段替换」正是靠段号对齐的。
DEAI_SYSTEM = (
    "你是中文小说的文字编辑。下面给你一节正文里的若干段落，每段用【段号】标出。"
    "这些段落有具体的行文问题，你要逐段改写。\n"
    "硬性要求：只改句式与用词，不改情节、不改人物说的话的意思、不改段落顺序；"
    "不增加也不删除信息；每一段改写后仍然是一整段。\n"
    "输出格式（只输出段号和正文，不要任何解释）：\n"
    "【5】\n改写后的这一段\n【9】\n改写后的这一段"
)

# 回复里可能有代码块、多余空行、甚至把段号写成「第 5 段」
_DEAI_PIECE = re.compile(r"【(\d+)】\s*\n(.*?)(?=\n*【\d+】|\Z)", re.S)


def deai_prompt(
    paras: list[tuple[int, str]], hits: list[dict], requirements: str = ""
) -> tuple[str, str]:
    """组装去 AI 味改写的 (system, user)。

    `paras` 要改的段落 [(段号, 原文)]，`hits` 是 aivoice 的命中清单。
    `requirements` 是文种要求：改写时「文体要求」仍然有效——不带它，
    模型会把小说段改得和通稿一样平，AI 味换了种而已。
    """
    system = DEAI_SYSTEM
    if (requirements or "").strip():
        system += "\n\n" + requirements.strip()

    blocks = [f"【{idx}】\n{para}" for idx, para in paras]
    problems: list[str] = []
    for hit in hits:
        loc = str(hit.get("location") or "").strip()
        where = f"第 {loc} 段" if loc else "本节"
        problems.append(f"- {where}：{hit.get('message') or ''}")
    user = ("要改的段落原文：\n" + "\n\n".join(blocks)
            + "\n\n问题清单：\n" + "\n".join(problems))
    return system, user


def parse_deai(text: str) -> dict[int, str]:
    """把「【段号】\n正文」的回复拆成 {段号: 正文}。"""
    out: dict[int, str] = {}
    for m in _DEAI_PIECE.finditer(text or ""):
        body = m.group(2).strip()
        if body:
            out[int(m.group(1))] = body
    return out
