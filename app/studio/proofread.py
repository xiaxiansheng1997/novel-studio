"""审校：客观排印规范 + 文风提示，全本地正则，零模型调用。

与软规则（`soft_rules.py`）的三点不同：

1. **always-on，不走 `checks` 开关**。排印规范与文种无关——不存在「历史文档
   可以用半角逗号」；而渠道层的 `checks` 是覆盖式写法，做成开关等于让「选了
   公众号」的作平白丢掉最基础的一层。
2. **级别**：叠字残留、半角标点这类几乎必是手滑，报 `warn`——但**不是
   error**，定稿闸门只认 error（`_final_gate`），排印瑕疵不该拦定稿的活；
   长句、的字堆叠、超长段落是文风问题，与软规则同级的 `info`。
3. **不进扣分链路**：`scoring._score_compliance` 数的是设定合规，排印噪音
   混进去只会稀释分数。

七条规则与各自的防误报设计：

| rule | 级别 | 判据 | 挡住什么 |
|---|---|---|---|
| `halfwidth_punct` | warn | 中文字符紧贴半角 `,;:?!` 或 `.` | 小数 `3.5`、版本号 `v1.2`、英文句 `word.` |
| `punct_unbalanced` | warn | 整节配对符号数量不等 | 各段自开自闭的多段对话整节算完是平衡的；一整段引语只在末段闭的连引写法**会报**，人确认即可 |
| `punct_cluster` | warn | `，。` `。。` 这类标点连打 | `？？` `！！` `？！` 是正当强调，跳过 |
| `char_repeat` | warn | 虚词叠字（「的的」「了了」） | 成语白名单：的的确确、是是非非 |
| `long_sentence` | info | 单句 ≥ 150 汉字 | 只报句长不评内容 |
| `de_density` | info | 长段落里「的」占比 ≥ 1/12 | 短段落不评，200 字起判 |
| `long_paragraph` | info | 段落 ≥ 500 汉字 | 移动端的阅读体验线，不是硬性错误 |

半角括号 `()` 不收：技术正文里 `中文 (x86)` 这类混排是正当写法，报出来
只会逼人关掉整组告警。每条规则每节最多报 2 处——审校刷屏比漏报更讨人嫌。
"""
from __future__ import annotations

import re

# 复用同一份代码剔除逻辑：技术类正文的代码块里全是半角标点，那不是「数据」也不是错别字。
# 与 assembler._fmt_outline 同一先例——宁可显式引私有名，也不要两处各抄一遍再漂移。
from app.studio.soft_rules import _strip_code

MAX_PER_RULE = 2

CJK = "\u4e00-\u9fff"
CJK_RE = re.compile(f"[{CJK}]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")
PARA_SPLIT = re.compile(r"\n\s*\n|\n")

# 句子级证据截取长度与告警面板的展示口径对齐
EVIDENCE_MAX = 160

_HW_RE = re.compile(
    f"(?:[{CJK}][,;:?!]|[,;:?!][{CJK}]"
    f"|[{CJK}]\\.(?![0-9A-Za-z.])"       # 中文结尾的半角句点："说完了."
    f"|(?<![0-9A-Za-z.])\\.[{CJK}])"     # 中文开头的半角句点；"." 挡住 "..." 接中文
)

# 成对符号：只按「同种开/闭计数不等」判，跨段引用（一段开、末段闭）整节算完就平衡。
PAIRS = (("“", "”"), ("‘", "’"), ("（", "）"), ("《", "》"), ("【", "】"),
         ("「", "」"), ("『", "』"))

_PUNCT_RUN_RE = re.compile(r"[，。！？；：、]{2,}")
# run 全部由 ！？ 组成是强调（"？？" "？！"），其余连打基本是手滑
_EMPHASIS = {"！", "？"}

# 虚词叠字：几乎只在「编辑时多删/少删一个字」时相邻出现，实词的重叠（谢谢、刚刚）
# 不在表内，正当构词进白名单。
_DUP_RE = re.compile(f"([{CJK}])\\1")
_DUP_FUNCTION = set("的了是在和就都很还过着被把让向于与及")
_DUP_OK_WORDS = ("的的确确", "是是非非")

LONG_SENT_CHARS = 150     # 单句汉字数
DE_MIN_PARA = 200         # 「的」密度起判段长
DE_RATIO = 12             # 每 12 个汉字最多 1 个「的」，超过即堆叠
LONG_PARA_CHARS = 500     # 超长段落线


# ==================== 小工具 ====================

def _paragraphs(content: str) -> list[tuple[int, str]]:
    """[(段号, 段落文本)]，与 consistency 的 citation_missing 同一套定位口径。"""
    out: list[tuple[int, str]] = []
    idx = 0
    for block in PARA_SPLIT.split(content or ""):
        text = block.strip()
        if not text:
            continue
        idx += 1
        out.append((idx, text))
    return out


def _cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text or ""))


def _snippet_at(text: str, pos: int, token: str) -> str:
    lo = max(0, pos - 24)
    return text[lo:pos + len(token) + 24].replace("\n", " ").strip()[:EVIDENCE_MAX]


def _issue(rule: str, severity: str, message: str, evidence: str = "",
           location: str = "") -> dict:
    return {"rule": rule, "severity": severity, "message": message,
            "evidence": evidence, "location": location}


# ==================== 规则 ====================

def _halfwidth(content: str) -> list[dict]:
    """中文语境里的半角标点。中英混排只在「紧贴汉字」时报，纯英文句子不动。"""
    out: list[dict] = []
    for idx, para in _paragraphs(content):
        m = _HW_RE.search(para)
        if not m:
            continue
        hits = len(_HW_RE.findall(para))
        out.append(_issue(
            "halfwidth_punct", "warn",
            f"第 {idx} 段有 {hits} 处半角标点贴着汉字（如「…{m.group(0)}…」），"
            "中文句读该用全角",
            _snippet_at(para, m.start(), m.group(0)), str(idx)))
        if len(out) >= MAX_PER_RULE:
            break
    return out


def _unbalanced(content: str) -> list[dict]:
    """整节配对的符号数量不等。按节不按段：连续对话每段开引号、只在末段闭，是规范写法。"""
    out: list[dict] = []
    for open_ch, close_ch in PAIRS:
        n_open, n_close = content.count(open_ch), content.count(close_ch)
        if n_open == n_close or (n_open == 0 and n_close == 0):
            continue
        lone = ""
        location = ""
        for idx, para in _paragraphs(content):
            if para.count(open_ch) != para.count(close_ch):
                location = str(idx)
                lone = f"——最先对不上的是第 {idx} 段：{para[:40]}"
                break
        # 每段各自平衡但整节不等（跨段引用）也报：数量摆在这里，人一眼能数完
        worse = "缺闭" if n_open > n_close else "缺开"
        out.append(_issue(
            "punct_unbalanced", "warn",
            f"「{open_ch}{close_ch}」在本节开了 {n_open} 个、闭了 {n_close} 个，{worse}了"
            f"{lone}",
            "", location))
        if len(out) >= MAX_PER_RULE:
            break
    return out


def _cluster(content: str) -> list[dict]:
    """标点连打：「，。」该留一个，「。。」该写成「……」。！？ 组成的强调跳过。"""
    out: list[dict] = []
    for idx, para in _paragraphs(content):
        for m in _PUNCT_RUN_RE.finditer(para):
            run = m.group(0)
            if set(run) <= _EMPHASIS:
                continue
            fix = "写成「……」" if set(run) == {"。"} else "留一个就好"
            out.append(_issue(
                "punct_cluster", "warn",
                f"第 {idx} 段里「{run}」是标点连着敲：{fix}",
                _snippet_at(para, m.start(), run), str(idx)))
            break
        if len(out) >= MAX_PER_RULE:
            break
    return out


def _dup_char(content: str) -> list[dict]:
    """虚词叠字，多半是改稿时多删/少删一个字留下的残渣。成语进白名单。"""
    out: list[dict] = []
    for idx, para in _paragraphs(content):
        for m in _DUP_RE.finditer(para):
            pair = m.group(0)
            if pair[0] not in _DUP_FUNCTION:
                continue
            lo = max(0, m.start() - 1)
            # 窗口取 4：白名单成语比叠字对多两侧各一字，取 3 会在成语开头位置漏判
            if any(w in para[lo:m.start() + 4] for w in _DUP_OK_WORDS):
                continue
            out.append(_issue(
                "char_repeat", "warn",
                f"第 {idx} 段出现「{pair}」：虚词叠字几乎总是手滑，"
                "请确认不是「的的确确」这类正当用法",
                _snippet_at(para, m.start(), pair), str(idx)))
            break
        if len(out) >= MAX_PER_RULE:
            break
    return out


def _long_sentence(content: str) -> list[dict]:
    out: list[dict] = []
    for idx, para in _paragraphs(content):
        for sent in SENT_SPLIT.split(para):
            n = _cjk_count(sent)
            if n < LONG_SENT_CHARS:
                continue
            out.append(_issue(
                "long_sentence", "info",
                f"第 {idx} 段有一句 {n} 字（≥{LONG_SENT_CHARS}）：不是错，"
                "但断在逗号处读者更好喘气",
                sent.strip()[:EVIDENCE_MAX], str(idx)))
            break
        if len(out) >= MAX_PER_RULE:
            break
    return out


def _de_density(content: str) -> list[dict]:
    out: list[dict] = []
    for idx, para in _paragraphs(content):
        total = _cjk_count(para)
        if total < DE_MIN_PARA:
            continue
        de = para.count("的")
        if de * DE_RATIO < total:
            continue
        out.append(_issue(
            "de_density", "info",
            f"第 {idx} 段 {total} 字里 {de} 个「的」（约每 {total // de} 字一个）："
            "堆叠读起来黏，挑几个改成删「的」后直接接中心词",
            para[:EVIDENCE_MAX], str(idx)))
        if len(out) >= MAX_PER_RULE:
            break
    return out


def _long_paragraph(content: str) -> list[dict]:
    out: list[dict] = []
    for idx, para in _paragraphs(content):
        n = _cjk_count(para)
        if n < LONG_PARA_CHARS:
            continue
        out.append(_issue(
            "long_paragraph", "info",
            f"第 {idx} 段有 {n} 字：手机屏上约十屏就是一块砖，"
            "按场景或时间切两三段更合适",
            para[:EVIDENCE_MAX], str(idx)))
        if len(out) >= MAX_PER_RULE:
            break
    return out


# ==================== 入口 ====================

PROOFREAD_RULES = ("halfwidth_punct", "punct_unbalanced", "punct_cluster",
                   "char_repeat", "long_sentence", "de_density", "long_paragraph")


def check_node(content: str, ordinal: int) -> list[dict]:
    """单节审校。纯同步、纯字符串，保存路径上每次都跑得起。

    `ordinal` 现在没用上（证据一律按段号定位），留着是为了和
    `soft_rules.check_node` 同形——以后要跨节对账不用改签名。
    """
    text = _strip_code(content or "")
    if _cjk_count(text) < 80:      # 太短的节（标题占位、半截稿）不值得评
        return []
    out: list[dict] = []
    for fn in (_halfwidth, _unbalanced, _cluster, _dup_char,
               _long_sentence, _de_density, _long_paragraph):
        out += fn(text)
    return out
