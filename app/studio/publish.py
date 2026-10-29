"""发布前检查：零模型调用的本地体检。

公众号的坑大多不在「写得对不对」，而在「发出去会不会被手机排版毁掉、会不会踩词」：
段落太长、中文句子用了半角标点、摘要没填导致正文前 54 字被截、结尾没有引导、
正文里放了点不动的外链、首段一上来就「随着…」、全文一处加粗都没有、
以及广告法/医疗/金融那几类风险词。这些都能用纯函数判出来，不该占一次模型调用。

命中风险词只提示「建议改写」，不拦截：词表会误报，判断权在作者。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from app.studio import wechat

BASE_DIR = Path(__file__).resolve().parent.parent.parent
RISK_PATH = BASE_DIR / "risk-words.yaml"

# 手机上一屏约 3 行、100 字左右；超过这个长度在公众号里就是一堵墙
LONG_PARA = 120
TITLE_SOFT = 20
TITLE_HARD = 64
DIGEST_MAX = 120
MIN_CHARS = 800
MAX_CHARS = 6000
MIN_SECTIONS = 2

# 手机打开后第一屏只放得下三行（约 60-80 字）；首段就是在这里被划走的
OPENING_LONG = 80
# 小标题超过这个长度会折成两行，看着像正文
HEADING_MAX = 20
# 短文章不用刻意加粗；过了这个长度还没有一处加粗，手机上就是一片灰
BOLD_MIN_CHARS = 1200

_CTA = ("留言", "在看", "转发", "关注", "点赞", "评论", "你呢", "你会", "？", "?")
# 首段套话：读者不用读第二行就知道后面要讲什么
_CLICHE = ("随着", "在当今", "众所周知", "近年来", "如今", "在这个",
           "我们生活在一个", "不可否认")
# 订阅号正文里的 http 链接点不动（只有「阅读原文」能跳），所以要单独报
_LINK = re.compile(r"https?://[^\s，。；、）)]+")


def _is_cjk(ch: str) -> bool:
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


def _snippet(text: str, pos: int, width: int = 1, pad: int = 18) -> str:
    left = max(0, pos - pad)
    right = min(len(text), pos + width + pad)
    return text[left:right].replace("\n", " ")


def _strip_code(text: str) -> str:
    """把代码块与行内代码换成等长空白：标点与段长不该按代码判。"""
    out = re.sub(r"```.*?```", lambda m: " " * len(m.group(0)), text, flags=re.S)
    return re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), out)


_risk_cache: dict[str, Any] = {"mtime": -1.0, "words": []}


def _load_risk() -> list[tuple[str, str]]:
    """读 risk-words.yaml，按 mtime 缓存；词表写坏了就当作空表，不能因此报错。"""
    path = Path(os.environ.get("W2A_RISK_WORDS") or RISK_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    if mtime == _risk_cache["mtime"]:
        return _risk_cache["words"]
    pairs: list[tuple[str, str]] = []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        data = {}
    for cat, items in (data.get("categories") or {}).items():
        for word in items or []:
            word = str(word).strip()
            if word:
                pairs.append((str(cat), word))
    _risk_cache.update({"mtime": mtime, "words": pairs})
    return pairs


def risk_hits(text: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """命中哪些风险词。同一个词只报一次，避免一段里重复刷屏。"""
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, word in _load_risk():
        if word in seen:
            continue
        pos = (text or "").find(word)
        if pos < 0:
            continue
        seen.add(word)
        hits.append({"category": category, "word": word,
                     "evidence": _snippet(text, pos, len(word))})
        if len(hits) >= limit:
            break
    return hits


def _halfwidth(text: str) -> Optional[tuple[int, str]]:
    """中文语境里的半角标点：前一个或后一个非空字符是汉字。"""
    for i, ch in enumerate(text):
        if ch not in ",;:!?":
            continue
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if _is_cjk(prev) or _is_cjk(nxt):
            return i, ch
    return None


def _opening(secs: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """第一篇有正文的节的第一段。首段决定读者走不走，所以只判这一处。"""
    for sec in secs:
        for block in wechat.split_blocks(_strip_code(sec.get("content") or "")):
            block = block.strip()
            if block and not block.startswith("```"):
                return sec, block
    return {}, ""


def _item(rule: str, severity: str, message: str, *, node_id: str = "",
          section: str = "", evidence: str = "") -> dict[str, Any]:
    return {
        "rule": rule,
        "severity": severity,
        "message": message,
        "node_id": node_id,
        "section": section,
        "evidence": evidence,
    }


def check(work: dict[str, Any], sections: Iterable[dict[str, Any]], *,
          digest: str = "") -> list[dict[str, Any]]:
    """返回按「硬 / 待确认」排好序的检查项。"""
    secs = list(sections)
    items: list[dict[str, Any]] = []
    stat = wechat.stats(secs)

    title = (work.get("title") or "").strip()
    if len(title) > TITLE_HARD:
        items.append(_item("title_too_long", "warn",
                           f"标题 {len(title)} 字，超过公众号上限 64 字"))
    elif len(title) > TITLE_SOFT:
        items.append(_item("title_long", "info",
                           f"标题 {len(title)} 字：列表页大约只显示前 20 字"))

    digest = (digest or "").strip()
    if not digest:
        items.append(_item("digest_missing", "warn",
                           "没填摘要：公众号会截正文前 54 字当摘要，常常截在半个句子上"))
    elif len(digest) > DIGEST_MAX:
        items.append(_item("digest_too_long", "warn",
                           f"摘要 {len(digest)} 字，超过 120 字会被折叠"))

    if len(secs) < MIN_SECTIONS:
        items.append(_item("too_few_sections", "warn",
                           f"只有 {len(secs)} 节：建议拆出 3-5 个小标题，手机上才不闷"))
    if stat["chars"] < MIN_CHARS:
        items.append(_item("too_short", "info",
                           f"全文 {stat['chars']} 字，偏短；公众号适合 1500-3000 字"))
    elif stat["chars"] > MAX_CHARS:
        items.append(_item("too_long", "info",
                           f"全文 {stat['chars']} 字，约 {stat['read_minutes']} 分钟；"
                           "超过 6000 字完读率会掉"))

    for sec in secs:
        nid = str(sec.get("id") or "")
        name = (sec.get("title") or "").strip()
        raw = sec.get("content") or ""
        text = _strip_code(raw)
        if len(name) > HEADING_MAX:
            items.append(_item(
                "heading_too_long", "info",
                f"小标题「{name[:12]}…」{len(name)} 字：手机上会折成两行，建议压到 "
                f"{HEADING_MAX} 字内",
                node_id=nid, section=name,
            ))
        if not text.strip():
            items.append(_item("empty_section", "warn", f"「{name or '未命名'}」还没有正文",
                               node_id=nid, section=name))
            continue

        for block in wechat.split_blocks(text):
            clean = re.sub(r"\s", "", block)
            if len(clean) > LONG_PARA:
                items.append(_item(
                    "long_paragraph", "warn",
                    f"「{name}」有一整段 {len(clean)} 字：手机上一屏约 3 行（100 字），建议拆开",
                    node_id=nid, section=name,
                    evidence=block.strip()[:30],
                ))
                break  # 每节只报一条，免得整篇刷成一片红

        bad = _halfwidth(text)
        if bad:
            pos, ch = bad
            items.append(_item(
                "halfwidth_punct", "info",
                f"「{name}」的中文句子里用了半角「{ch}」，公众号正文里通常是全角",
                node_id=nid, section=name, evidence=_snippet(text, pos, 1),
            ))

        link = _LINK.search(text)
        if link:
            items.append(_item(
                "external_link", "warn",
                f"「{name}」里有外链：订阅号正文的链接点不动，"
                "要么改成「公众号文章标题」，要么放进「阅读原文」",
                node_id=nid, section=name,
                evidence=_snippet(text, link.start(), len(link.group(0))),
            ))

        if re.search(r"\n[ \t]*\n[ \t]*\n", raw):
            items.append(_item("double_blank", "info", f"「{name}」里有连续空行",
                               node_id=nid, section=name))
        if any(ln != ln.rstrip() for ln in raw.splitlines()):
            items.append(_item("trailing_space", "info", f"「{name}」里有行尾空格",
                               node_id=nid, section=name))

        for hit in risk_hits(text):
            items.append(_item(
                "risk_word", "warn",
                f"「{name}」出现「{hit['word']}」（{hit['category']}），发布前建议改写",
                node_id=nid, section=name, evidence=hit["evidence"],
            ))

    first, opening = _opening(secs)
    if opening:
        clean_open = re.sub(r"\s", "", opening)
        cliche = next((w for w in _CLICHE if opening.startswith(w)), "")
        if cliche:
            items.append(_item(
                "weak_opening", "warn",
                f"首段以「{cliche}」开头：读者第一屏就在决定走不走，直接给场景或结论",
                node_id=str(first.get("id") or ""),
                section=(first.get("title") or "").strip(),
                evidence=opening[:24],
            ))
        elif len(clean_open) > OPENING_LONG:
            items.append(_item(
                "weak_opening", "info",
                f"首段 {len(clean_open)} 字：手机第一屏只放得下三行，建议压到 "
                f"{OPENING_LONG} 字内",
                node_id=str(first.get("id") or ""),
                section=(first.get("title") or "").strip(),
                evidence=opening[:24],
            ))

    if stat["chars"] > BOLD_MIN_CHARS and not any(
        "**" in (s.get("content") or "") for s in secs
    ):
        items.append(_item(
            "no_bold", "info",
            "全文没有一处加粗：公众号靠加粗关键句撑节奏，每 2-3 段挑一句判断句加粗",
        ))

    if secs:
        last = _strip_code(secs[-1].get("content") or "")
        if not any(k in last[-200:] for k in _CTA):
            items.append(_item(
                "no_cta", "info",
                "结尾没有互动或关注引导：问一句、请留言、点个在看都算",
                node_id=str(secs[-1].get("id") or ""),
                section=(secs[-1].get("title") or "").strip(),
            ))

    if stat["chars"] > 1200 and not wechat.extract_images(secs):
        items.append(_item(
            "no_image", "info",
            "正文里没有配图占位：每 2-3 段插一个「[图：画面]」，导出时会变成占位块",
        ))

    # 硬问题在前、待确认在后；同类保持发现顺序
    items.sort(key=lambda it: 0 if it["severity"] == "warn" else 1)
    return items[:40]
