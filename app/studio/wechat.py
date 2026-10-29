"""公众号排版：把正文渲染成微信编辑器认得的 HTML。

公众号编辑器会剥掉 `<style>`、class 与外链样式表，只保留**内联 style**，
所以这里每条规则都写成内联；也不做「全局样式表」那种优雅但会被吃掉的做法。

支持的 Markdown 子集只覆盖本系统会产出的那几种：段落、`**加粗**`、`` `代码` ``、
`[文字](链接)`、`- ` / `1. ` 列表、`> ` 引用、``` 代码块，以及配图占位
`[图：画面描述]`。其余一律按纯文本处理——宁可少认，也不要认错把正文吃掉。
"""
from __future__ import annotations

import html
import math
import re
from typing import Any, Iterable, Optional

# 三套版式。公众号会剥掉 <style> 与 class，只留内联样式，
# 所以「主题」在这里就是一组配色与间距参数，渲染时展开成 style 字符串。
# 之所以要换版式：不同栏目的气质不一样，而每篇都长一个样，读者会当成同一篇。
_THEME_SPECS: dict[str, dict[str, object]] = {
    "paper": {
        "name": "纸张", "body": "#333", "head": "#1a1a1a", "size": 16, "lh": 1.75,
        "para_gap": 1.5, "h2_top": "2em", "quote_bg": "#faf7f2",
        "quote_border": "#d9a45b", "quote_color": "#555", "pre_bg": "#f6f6f6",
        "code_bg": "#f2f2f2", "code_color": "#c7254e", "link": "#576b95",
        "link_border": "#d6d6d6", "img_border": "#c9c9c9", "img_bg": "#fcfcfc",
        "muted": "#8a8a8a",
    },
    "mono": {
        "name": "极简", "body": "#222", "head": "#000", "size": 16, "lh": 1.8,
        "para_gap": 1.6, "h2_top": "2.2em", "quote_bg": "#f7f7f7",
        "quote_border": "#222", "quote_color": "#444", "pre_bg": "#f5f5f5",
        "code_bg": "#eeeeee", "code_color": "#111", "link": "#333",
        "link_border": "#bbbbbb", "img_border": "#dddddd", "img_bg": "#fafafa",
        "muted": "#888888",
    },
    "warm": {
        "name": "暖色", "body": "#3a3330", "head": "#8b5e3c", "size": 16, "lh": 1.85,
        "para_gap": 1.7, "h2_top": "2em", "quote_bg": "#fdf6ee",
        "quote_border": "#e0b184", "quote_color": "#6b5a4e", "pre_bg": "#faf4ec",
        "code_bg": "#f6ece1", "code_color": "#a0522d", "link": "#8b5e3c",
        "link_border": "#e0d3c6", "img_border": "#e8d8c6", "img_bg": "#fffaf3",
        "muted": "#a08a78",
    },
}

DEFAULT_THEME = "paper"
THEME_LABELS = {k: str(v["name"]) for k, v in _THEME_SPECS.items()}


def styles(theme: str = DEFAULT_THEME) -> dict[str, str]:
    """把一套版式参数展开成一包内联样式。未知主题退回默认，不报错。"""
    t = _THEME_SPECS.get(theme) or _THEME_SPECS[DEFAULT_THEME]
    size = int(t["size"])  # type: ignore[arg-type]
    lh = t["lh"]
    gap = t["para_gap"]
    return {
        "outer": f"font-size:{size}px;color:{t['body']};line-height:{lh}",
        "h1": (f"margin:0 0 1em;font-size:{size + 6}px;line-height:1.4;"
               f"font-weight:700;color:{t['head']};letter-spacing:0.5px"),
        "h2": (f"margin:{t['h2_top']} 0 1em;font-size:{size + 2}px;line-height:1.5;"
               f"font-weight:700;color:{t['head']};letter-spacing:0.5px"),
        "p": (f"margin:0 0 {gap}em;font-size:{size}px;line-height:{lh};"
              f"letter-spacing:0.5px;color:{t['body']};text-align:justify;"
              "word-break:break-word"),
        "quote": (f"margin:0 0 {gap}em;padding:0.9em 1em;"
                  f"border-left:3px solid {t['quote_border']};"
                  f"background:{t['quote_bg']};font-size:{size - 1}px;"
                  f"line-height:1.7;color:{t['quote_color']}"),
        "pre": (f"margin:0 0 {gap}em;padding:1em;background:{t['pre_bg']};"
                "border-radius:6px;font-size:13px;line-height:1.6;"
                f"color:{t['body']};white-space:pre-wrap;word-break:break-all;"
                "overflow-x:auto"),
        "code": (f"padding:0.1em 0.35em;background:{t['code_bg']};"
                 f"border-radius:3px;font-size:14px;color:{t['code_color']}"),
        "a": (f"color:{t['link']};text-decoration:none;"
              f"border-bottom:1px solid {t['link_border']}"),
        "list": (f"margin:0 0 {gap}em;padding-left:1.4em;font-size:{size}px;"
                 f"line-height:{lh};color:{t['body']}"),
        "li": "margin:0 0 0.5em",
        "img": (f"margin:0 0 {gap}em;padding:20px 12px;border:1px dashed "
                f"{t['img_border']};border-radius:6px;text-align:center;"
                f"font-size:13px;line-height:1.6;color:{t['muted']};"
                f"background:{t['img_bg']}"),
        "footer": (f"margin:2em 0 0;padding-top:1.2em;border-top:1px solid "
                   f"{t['img_border']};font-size:13px;line-height:1.9;"
                   f"color:{t['muted']};text-align:center"),
        "archive": (f"margin:1.4em 0 0;font-size:13px;line-height:1.9;"
                    f"color:{t['muted']};text-align:center"),
    }

# [图：xxx] / 【配图：xxx】——只有整段就是它的时候才算占位，句中出现不处理
_IMG_LINE = re.compile(r"^\s*[\[【]\s*(?:配图|图)\s*[:：]\s*(.+?)\s*[\]】]\s*$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_FENCE = "```"


def _inline(text: str, s: dict[str, str]) -> str:
    """行内格式：先转义再套标签，避免正文里的尖括号被当成 HTML。"""
    out = html.escape(text, quote=False)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _INLINE_CODE.sub(lambda m: f'<code style="{s["code"]}">{m.group(1)}</code>', out)
    out = _LINK.sub(
        lambda m: (
            f'<a style="{s["a"]}" href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>'
        ),
        out,
    )
    return out


def split_blocks(text: str) -> list[str]:
    """按空行切块；代码围栏里的空行不算分块。

    不引 Markdown 库的原因：产出格式只有这几种，而真正要调的（行距、段距、
    手机宽度下的断行）都得手写内联样式，库帮不上忙，反而多一个依赖。
    """
    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in (text or "").splitlines():
        if line.strip().startswith(_FENCE):
            in_fence = not in_fence
            buf.append(line)
            if not in_fence:
                blocks.append("\n".join(buf))
                buf = []
            continue
        if in_fence:
            buf.append(line)
        elif not line.strip():
            if buf:
                blocks.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        blocks.append("\n".join(buf))
    return blocks


def image_desc(block: str) -> str:
    """整段就是一个配图占位时返回描述，否则返回空串。"""
    if "\n" in block.strip():
        return ""
    m = _IMG_LINE.match(block.strip())
    return m.group(1).strip() if m else ""


def _render_block(block: str, s: dict[str, str]) -> str:
    lines = block.splitlines()
    if lines and lines[0].strip().startswith(_FENCE):
        body = "\n".join(lines[1:])
        if body.rstrip().endswith(_FENCE):
            body = body.rstrip()[: -len(_FENCE)]
        return f'<pre style="{s["pre"]}"><code>{html.escape(body.strip(chr(10)))}</code></pre>'

    desc = image_desc(block)
    if desc:
        # 图片必须先在公众号后台上传，外链图会被屏蔽；这里只留一个显眼的占位
        return f'<section style="{s["img"]}">配图：{html.escape(desc, quote=False)}</section>'

    if lines and all(ln.strip().startswith(">") for ln in lines):
        inner = "\n".join(ln.lstrip()[1:].lstrip() for ln in lines)
        return f'<blockquote style="{s["quote"]}">{_inline(inner, s)}</blockquote>'

    bullets = [_BULLET.match(ln) for ln in lines]
    if lines and all(bullets):
        items = "".join(
            f'<li style="{s["li"]}">{_inline(m.group(1), s)}</li>' for m in bullets
        )
        return f'<ul style="{s["list"]}">{items}</ul>'

    ordered = [_ORDERED.match(ln) for ln in lines]
    if lines and all(ordered):
        items = "".join(
            f'<li style="{s["li"]}">{_inline(m.group(1), s)}</li>' for m in ordered
        )
        return f'<ol style="{s["list"]}">{items}</ol>'

    # 段内换行在公众号里要显式 <br/>，否则会被并成一行
    body = "<br/>".join(_inline(ln, s) for ln in lines)
    return f'<p style="{s["p"]}">{body}</p>'


def render_article(
    sections: Iterable[dict[str, Any]], *, title: str = "", include_title: bool = False,
    theme: str = DEFAULT_THEME, footer: str = "",
    archive: Optional[Iterable[dict[str, Any]]] = None,
) -> str:
    """把 [{title, content}] 渲染成一篇可直接粘进公众号的 HTML。

    标题默认不输出：公众号的标题是后台单独填的，正文里再来一个只是重复。
    `footer` 是固定的文末引导（每篇都一样的那句「点个关注」）；
    `archive` 是往期推荐，只放标题与链接，没有链接的条目直接跳过。
    """
    s = styles(theme)
    parts: list[str] = []
    if include_title and title.strip():
        parts.append(f'<h1 style="{s["h1"]}">{html.escape(title.strip(), quote=False)}</h1>')
    for sec in sections:
        name = (sec.get("title") or "").strip()
        if name:
            parts.append(f'<h2 style="{s["h2"]}">{html.escape(name, quote=False)}</h2>')
        for block in split_blocks(sec.get("content") or ""):
            parts.append(_render_block(block, s))

    if footer.strip():
        lines = [ln for ln in footer.strip().splitlines() if ln.strip()]
        parts.append(
            f'<section style="{s["footer"]}">'
            + "<br/>".join(_inline(ln.strip(), s) for ln in lines)
            + "</section>"
        )

    past = [a for a in (archive or []) if a.get("url")]
    if past:
        items = "<br/>".join(
            f'<a style="{s["a"]}" href="{html.escape(str(a.get("url")), quote=True)}">'
            f'{html.escape(str(a.get("title") or "无标题"), quote=False)}</a>'
            for a in past[:5]
        )
        parts.append(f'<section style="{s["archive"]}">往期推荐<br/>{items}</section>')

    return f'<section style="{s["outer"]}">' + "\n".join(parts) + "</section>"


def extract_images(sections: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """配图清单：哪一节、要什么画面。图片本身得在公众号后台传。"""
    out: list[dict[str, Any]] = []
    for sec in sections:
        for block in split_blocks(sec.get("content") or ""):
            desc = image_desc(block)
            if desc:
                out.append({
                    "node_id": sec.get("id") or "",
                    "section": sec.get("title") or "",
                    "desc": desc,
                })
    return out


def stats(sections: Iterable[dict[str, Any]]) -> dict[str, int]:
    """字数与阅读时长。按非空白字符算，和公众号后台的口径接近。"""
    chars = 0
    paragraphs = 0
    count = 0
    for sec in sections:
        count += 1
        for block in split_blocks(sec.get("content") or ""):
            if not block.strip() or block.strip().startswith(_FENCE):
                continue
            if image_desc(block):
                # 配图占位最后会被换成图，不该算进字数和段数
                continue
            chars += len(re.sub(r"\s", "", block))
            paragraphs += 1
    return {
        "sections": count,
        "chars": chars,
        "paragraphs": paragraphs,
        # 中文阅读约 400 字/分钟；不足一分钟也给 1
        "read_minutes": max(1, math.ceil(chars / 400)) if chars else 0,
    }
