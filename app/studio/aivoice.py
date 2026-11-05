"""AI 味检测：本地统计九个可量化特征，零模型调用。

为什么单独一层：`proofread` 查的是「错」（半角标点、符号不配对），`soft_rules`
查的是「与设定不符」，而 AI 味两者都不是——单看每一句都挑不出错，是整节的
**分布**偏了：破折号比真人密三倍、每段都收在总结句上、情绪全靠形容词直说。
这类问题只能靠频率判，不能靠「出现即报」。

四条刻意的约束：

1. **只对小说生效**（`consistency.run_checks` 里按 kind 分叉）。说明文与公文
   本来就该有总结句和排比，拿这套去量只会逼人关掉整组告警。
2. **一律 info**，不拦定稿、不进扣分——与软规则同级。AI 味是「请看一眼」，
   不是「你写错了」。
3. **阈值先宽后紧**：首版按真人小说的常见水平放宽到约两倍取整。宁可漏报，
   也不要一上来满屏误报——刷屏比漏报更快让人失去信任。想收紧时先拿几本
   同类型真人稿跑一遍，按分位数改常量；`tests/test_aivoice.py` 里每条规则
   的「该报 / 不该报」对照用例就是改完之后的防误报门槛。
4. **每条规则每节只报一处**，且 message 必须带「实测值 / 阈值」。不带数字
   的告警既不知道改到哪算够，也没法对比改前改后。

七条规则与判据：

| rule | 可观测量 | 宽阈值 |
|---|---|---|
| `dash_explain` | `——` 每千字次数 | > 4 |
| `parallel_triplet` | 同一段里的排比句式数 | ≥ 2 |
| `tail_summary` | 段末句以「他知道/这意味着」开头 | 占比 > 30% 且 ≥5 段 |
| `emotion_label` | 情绪直述词每千字 | > 3 |
| `hedge_stack` | 仿佛/似乎/像是/好像 每千字 | > 5 |
| `sensory_cliche` | 万能意象短语 | ≥ 2 处 |
| `filler_adverb` | 缓缓/轻轻/静静/深深/淡淡/微微 每千字 | > 6 |
| `transition_filler` | 就在这时/与此同时/片刻之后 每千字 | > 3 |
| `dialogue_ratio` | 引号内汉字占全文比例 | < 12% |

前七条量的是**词汇与句式**，后两条量的是**场景节奏**：一个是换镜头的
套话密度，一个是「这一节有没有人说话」。两条都只给 info，因为节奏
问题没有对错，只有像不像小说。
"""
from __future__ import annotations

import re

from app.studio.proofread import EVIDENCE_MAX, SENT_SPLIT, _cjk_count, _paragraphs
from app.studio.soft_rules import _strip_code

# 少于 300 汉字不评：频率类指标在小样本上抖得厉害，一节 200 字里出现
# 两次「仿佛」判成「每千字 10 次」毫无意义。
MIN_CHARS = 300
# 频率类告警报一处就够：同一个数字重复五遍不会让人改得更快
MAX_PER_RULE = 1

DASH_PER_K = 4.0
PARALLEL_PER_PARA = 2
TAIL_MIN_PARAS = 5
TAIL_RATIO = 0.30
EMOTION_PER_K = 3.0
HEDGE_PER_K = 5.0
CLICHE_MIN = 2
ADVERB_PER_K = 6.0
TRANSITION_PER_K = 3.0
DIALOGUE_MIN_RATIO = 0.12
# 短节常常就是过场或场景切片，一句对白都没有很正常。800 字以上还全是
# 转述，才是「靠叙述推情节」的真问题——这也是它不跟 MIN_CHARS 一起走的原因。
DIALOGUE_MIN_CHARS = 800

# 情绪直述：把「心里怎么想」直接写出来的词。筛词标准是「AI 爱用而真人
# 小说很少用」；「心中一紧」「不禁」这类硬汉派常用语故意不收，收了必然误报。
_EMOTION = (
    "五味杂陈", "百感交集", "涌上心头", "难以言喻", "无以言表",
    "眼神复杂", "莫名地", "说不出的", "形容不出", "心里五味",
)

# 模糊限定词：单个都是正当修辞，堆起来才是 AI 味的来源
_HEDGE = ("仿佛", "似乎", "像是", "好像", "宛若", "宛如")

# 万能意象：整串匹配，一个就够可疑，两个以上基本可以确定
_CLICHE = (
    "空气中弥漫着", "时间仿佛静止", "仿佛能听见", "仿佛能感受到",
    "能听见自己的心跳", "安静得可怕", "落针可闻", "如潮水般涌来",
    "像被抽空了力气", "声音里带着一丝", "空气仿佛凝固",
)

# 装饰性副词：单用无妨，密度过高就是「每一步都轻轻地」的木偶戏
_ADVERB = ("缓缓", "轻轻", "静静", "深深", "淡淡", "微微")

# 转场套话：换镜头时用来「接上」的固定说法，密了就是分镜脚本
_TRANSITION = ("就在这时", "与此同时", "片刻之后", "良久", "话音刚落",
               "紧接着", "不知过了多久", "下一秒", "转眼间")

# 对白：不跨行，避免一个没闭合的引号把后面几千字全当成对话
_DIALOGUE_RE = re.compile(r"[「“]([^」”\n]{1,300})[」”]")

# 排比三连：AI 的默认节奏。「既(?!然)」是为了放过「既然…又」这种正常句式
_PARALLEL = (
    re.compile(r"不是[^。！？\n]{2,20}?而是"),
    re.compile(r"不仅[^。！？\n]{2,20}?(?:而且|还)"),
    re.compile(r"之所以[^。！？\n]{2,20}?是因为"),
    re.compile(r"既(?!然)[^。！？\n]{1,16}?又"),
)

# 段末总结句：AI 每段都要升华一下，真人小说很少段段如此
_TAIL = re.compile(
    r"^(?:这意味着|这就是|从这一刻起|从此(?:，|,)?|"
    r"(?:他|她)(?:知道|明白|意识到|终于明白))"
)


def _per_k(n: int, chars: int) -> float:
    return round(n * 1000.0 / chars, 1) if chars else 0.0


def _issue(message: str, evidence: str, location: str = "") -> dict:
    return {"rule": "", "severity": "info", "message": message,
            "evidence": evidence, "location": location}


def _snippet(text: str, needle: str) -> str:
    pos = text.find(needle)
    if pos < 0:
        return ""
    lo = max(0, pos - 20)
    return text[lo:pos + len(needle) + 20].replace("\n", " ").strip()[:EVIDENCE_MAX]


def _para_of(text: str, needle: str) -> str:
    """命中词落在第几段。

    词汇类规则（破折号、情绪词、副词…）量的是全节密度，但落到面板上
    必须能「定位」——否则用户知道「有 8 个」却不知道去哪找，而
    `tasks.run_deai` 也要靠这个段号决定重写哪几段。
    """
    if not needle:
        return ""
    for idx, para in _paragraphs(text):
        if needle in para:
            return str(idx)
    return ""


def _dash(content: str, chars: int) -> list[dict]:
    """破折号密度。单看「有没有」不算问题——对话中断就该用它，密了才是。"""
    n = content.count("——")
    per_k = _per_k(n, chars)
    if per_k <= DASH_PER_K:
        return []
    got = _issue(
        f"「——」出现 {n} 处（每千字 {per_k} 个，阈值 {DASH_PER_K}）："
        "破折号用来做解释或递进是最典型的 AI 腔，能拆成句号就拆",
        _snippet(content, "——"), _para_of(content, "——"))
    got["rule"] = "dash_explain"
    return [got]


def _parallel(content: str, chars: int) -> list[dict]:
    """同段内的排比句式数量。一段里凑齐两种，读者会听出「这是机器在造句」。"""
    for idx, para in _paragraphs(content):
        hits: list[str] = []
        for pat in _PARALLEL:
            hits.extend(m.group(0) for m in pat.finditer(para))
        if len(hits) < PARALLEL_PER_PARA:
            continue
        got = _issue(
            f"第 {idx} 段有 {len(hits)} 处排比句式（{'；'.join(hits[:3])}）："
            "同一段里两种以上「不是…而是 / 既…又 / 不仅…而且」会让节奏变整齐，"
            "留一处、其余拆成直述",
            para[:EVIDENCE_MAX], str(idx))
        got["rule"] = "parallel_triplet"
        return [got]
    return []


def _tail(content: str, chars: int) -> list[dict]:
    """段末总结句占比。逐段看「最后一句是不是在替读者下结论」。"""
    paras = _paragraphs(content)
    if len(paras) < TAIL_MIN_PARAS:
        return []
    hit: tuple[int, str] | None = None
    hits = 0
    for idx, para in paras:
        sents = [s.strip() for s in SENT_SPLIT.split(para) if s.strip()]
        if not sents:
            continue
        last = sents[-1]
        if _TAIL.match(last):
            hits += 1
            if hit is None:
                hit = (idx, last)
    ratio = hits / len(paras)
    if not hit or ratio <= TAIL_RATIO:
        return []
    got = _issue(
        f"{len(paras)} 段里有 {hits} 段收在总结句上（{round(ratio * 100)}%，"
        f"阈值 {round(TAIL_RATIO * 100)}%）：段段都「他知道」「这意味着」，"
        "等于每段都替读者把意思点破，删掉结论、让动作停在画面上",
        hit[1][:EVIDENCE_MAX], str(hit[0]))
    got["rule"] = "tail_summary"
    return [got]


def _emotion(content: str, chars: int) -> list[dict]:
    """情绪直述词密度。"""
    n = sum(content.count(w) for w in _EMOTION)
    per_k = _per_k(n, chars)
    if per_k <= EMOTION_PER_K:
        return []
    word = next((w for w in _EMOTION if w in content), "")
    got = _issue(
        f"情绪直述词出现 {n} 次（每千字 {per_k} 个，阈值 {EMOTION_PER_K}）："
        "「五味杂陈」「涌上心头」这类是把情绪直接报给读者，换成身体反应"
        "（手停半拍、喉咙发紧、把杯子放下）",
        _snippet(content, word) if word else "", _para_of(content, word))
    got["rule"] = "emotion_label"
    return [got]


def _hedge(content: str, chars: int) -> list[dict]:
    """模糊限定词密度。"""
    n = sum(content.count(w) for w in _HEDGE)
    per_k = _per_k(n, chars)
    if per_k <= HEDGE_PER_K:
        return []
    word = next((w for w in _HEDGE if w in content), "")
    got = _issue(
        f"「仿佛/似乎/像是」共 {n} 个（每千字 {per_k} 个，阈值 {HEDGE_PER_K}）："
        "连续用模糊词说明写的人不敢肯定画面，挑最实的那一个直接写出来",
        _snippet(content, word) if word else "", _para_of(content, word))
    got["rule"] = "hedge_stack"
    return [got]


def _cliche(content: str, chars: int) -> list[dict]:
    """万能意象短语。"""
    hits = [w for w in _CLICHE if w in content]
    if len(hits) < CLICHE_MIN:
        return []
    got = _issue(
        f"万能意象 {len(hits)} 处（{'、'.join(hits[:3])}）："
        "这几句放进任何一本小说都成立，正说明它没提供信息；换成本书独有的物件或声音",
        _snippet(content, hits[0]), _para_of(content, hits[0]))
    got["rule"] = "sensory_cliche"
    return [got]


def _adverb(content: str, chars: int) -> list[dict]:
    """装饰性副词密度。"""
    n = sum(content.count(w) for w in _ADVERB)
    per_k = _per_k(n, chars)
    if per_k <= ADVERB_PER_K:
        return []
    word = next((w for w in _ADVERB if w in content), "")
    got = _issue(
        f"「缓缓/轻轻/静静」类副词共 {n} 个（每千字 {per_k} 个，阈值 {ADVERB_PER_K}）："
        "副词替动作定性，读者反而看不见动作；删掉副词，让动词自己说话",
        _snippet(content, word) if word else "", _para_of(content, word))
    got["rule"] = "filler_adverb"
    return [got]


def _transition(content: str, chars: int) -> list[dict]:
    """转场套话密度。换镜头靠「就在这时」接，读者听到的是分镜旁白。"""
    n = sum(content.count(w) for w in _TRANSITION)
    per_k = _per_k(n, chars)
    if per_k <= TRANSITION_PER_K:
        return []
    word = next((w for w in _TRANSITION if w in content), "")
    got = _issue(
        f"转场套话出现 {n} 次（每千字 {per_k} 个，阈值 {TRANSITION_PER_K}）："
        "「就在这时」「与此同时」「片刻之后」是在告诉读者「我换镜头了」，"
        "直接空行换场，或用人物动作带过去",
        _snippet(content, word) if word else "", _para_of(content, word))
    got["rule"] = "transition_filler"
    return [got]


def _dialogue(content: str, chars: int) -> list[dict]:
    """对白占比。小说靠人说话推进，全靠转述的章节读起来像大纲。"""
    if chars < DIALOGUE_MIN_CHARS:
        return []
    spoke = sum(_cjk_count(m.group(1)) for m in _DIALOGUE_RE.finditer(content))
    ratio = round(spoke / chars, 3) if chars else 0.0
    if ratio >= DIALOGUE_MIN_RATIO:
        return []
    got = _issue(
        f"本节 {chars} 字里对白只有 {spoke} 字（{round(ratio * 100)}%，"
        f"阈值 {round(DIALOGUE_MIN_RATIO * 100)}%）：冲突与信息全靠叙述转述，"
        "读者听不到人物怎么说话；至少把一处关键交锋改成当面对白（本来就是独白的节可忽略）",
        "")
    got["rule"] = "dialogue_ratio"
    return [got]


AVOICE_RULES = ("dash_explain", "parallel_triplet", "tail_summary",
                "emotion_label", "hedge_stack", "sensory_cliche",
                "filler_adverb", "transition_filler", "dialogue_ratio")

# 规则 → 命中词表。`tasks.run_deai` 靠它把「本节破了阈值」还原成
# 「到底哪几段有这东西」：命中项只带一处的证据，但去 AI 味要改全。
# parallel_triplet / tail_summary 不在这里，它们的段号就在命中项里。
RULE_WORDS: dict[str, tuple[str, ...]] = {
    "dash_explain": ("——",),
    "emotion_label": _EMOTION,
    "hedge_stack": _HEDGE,
    "sensory_cliche": _CLICHE,
    "filler_adverb": _ADVERB,
    "transition_filler": _TRANSITION,
}


def paragraphs_with(content: str, rule: str, limit: int = 8) -> list[int]:
    """某条词汇类规则在哪些段落里出现过（段号升序）。"""
    words = RULE_WORDS.get(rule)
    if not words:
        return []
    out: list[int] = []
    for idx, para in _paragraphs(content):
        if any(w in para for w in words):
            out.append(idx)
            if len(out) >= limit:
                break
    return out


def check_node(content: str, ordinal: int = 0) -> list[dict]:
    """单节 AI 味检测。纯同步、纯字符串，每次保存都跑得起。

    `ordinal` 目前没用上（证据按段号定位），留着与 `proofread.check_node`
    同形——以后要跨节对账（比如连续三节都用同一套意象）不用改签名。

    段号是在**去掉代码块之后**的文本上算的（换行保留，所以正文行号不漂），
    调用方若要拿它在原文里定位，得像 `tasks._deai_targets` 那样重新对齐。
    """
    text = _strip_code(content or "")
    chars = _cjk_count(text)
    if chars < MIN_CHARS:
        return []
    out: list[dict] = []
    for fn in (_dash, _parallel, _tail, _emotion, _hedge, _cliche, _adverb,
               _transition, _dialogue):
        out += fn(text, chars)[:MAX_PER_RULE]
    return out
