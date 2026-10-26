"""中文分词：FTS5 的 unicode61 不切中文，必须预分词。

为什么需要这一层：SQLite 自带的 unicode61 分词器把连续中文当成一个 token，
`MATCH '青云宗'` 只能命中「青云宗」这四个字连在一起的片段，命中「青云宗弟子」
可以（前缀），但命中「他回到了青云宗」就不行——整段是一个 token。
所以写入时先用 jieba 切成空格分隔的词串存进 `text_tok`，检索时用同一套切法，
倒排才可能对上。

两个坑：
1. jieba 首次加载词典约 0.7 秒，不能放在模块导入期，否则拖慢服务启动
2. 专有名词（人名、地名、术语）默认会被切碎（「青云宗」→「青云」「宗」），
   所以提供 `add_words()`，由调用方把作品的术语表灌进来
"""
from __future__ import annotations

import re
import sys

# 首次调用时才 import + initialize，避免拖慢服务启动
_jieba = None
_jieba_ready = False

# 高频虚词：留在倒排里只会稀释 BM25，直接丢掉
_STOPWORDS = {
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们", "和", "与", "或",
    "也", "就", "都", "而", "及", "着", "过", "把", "被", "让", "对", "从", "到",
    "有", "没有", "这", "那", "这个", "那个", "一个", "一些", "什么", "怎么",
    "可以", "已经", "还是", "但是", "因为", "所以", "如果", "不过", "只是",
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "and", "or",
}

# 只保留中文、字母、数字；标点与空白一律丢弃
_KEEP = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _ensure_jieba():
    global _jieba, _jieba_ready
    if _jieba_ready:
        return _jieba
    try:
        import jieba  # 延迟导入

        jieba.setLogLevel(20)  # WARNING 以上才打，避免每次启动刷屏
        # 关掉「的」「了」这类会被单独切出来的功能词？（jieba 无此开关，靠停用词表）
        _jieba = jieba
    except Exception:  # pragma: no cover - 纯防御，没装 jieba 也不能让检索崩
        print("[tokenize] 未安装 jieba，退回字符 bigram 分词", file=sys.stderr)
        _jieba = None
    _jieba_ready = True
    return _jieba


def add_words(words: list[str]) -> None:
    """把专有名词加进词典。重复调用是幂等的。"""
    jieba = _ensure_jieba()
    if not jieba:
        return
    for w in words:
        w = (w or "").strip()
        if len(w) >= 2:
            jieba.add_word(w, freq=2000)


def _bigrams(text: str) -> list[str]:
    """没有 jieba 时的兜底：中文按 2-gram，英文按词。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
            if ch.isspace() or ch in "，。！？；：、,.!?;:":
                if buf.strip():
                    out.append(buf.strip())
                buf = ""
    if buf.strip():
        out.append(buf.strip())
    grams: list[str] = []
    for i, tok in enumerate(out):
        if len(tok) == 1 and "\u4e00" <= tok <= "\u9fff" and i + 1 < len(out):
            nxt = out[i + 1]
            if len(nxt) == 1 and "\u4e00" <= nxt <= "\u9fff":
                grams.append(tok + nxt)
    return grams or out


def tokens(text: str) -> list[str]:
    """切词并过滤停用词/单字噪声。"""
    if not text:
        return []
    jieba = _ensure_jieba()
    if jieba is None:
        raw = _bigrams(text)
    else:
        raw = list(jieba.cut(text))

    out: list[str] = []
    for tok in raw:
        tok = tok.strip()
        if not tok:
            continue
        # 纯标点直接丢
        if not _KEEP.search(tok):
            continue
        low = tok.lower()
        if low in _STOPWORDS:
            continue
        if len(tok) == 1 and not tok.isascii():
            # 单个汉字信息量太低，只在没有别的词时兜底
            continue
        out.append(tok)
    return out


def tokenize(text: str) -> str:
    """写入 `chunks.text_tok` 用：空格分隔的词串。"""
    return " ".join(tokens(text))


def match_query(query: str, *, max_terms: int = 12) -> str:
    """把自然语言 query 变成 FTS5 的 MATCH 表达式。

    每个词单独加双引号，避免 `-`、`*`、`:` 被当成 FTS 语法；
    词之间用 OR（AND 会过于严格，长 query 常常零命中）。
    """
    uniq: list[str] = []
    for tok in tokens(query):
        if tok not in uniq:
            uniq.append(tok)
        if len(uniq) >= max_terms:
            break
    if not uniq:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in uniq)
