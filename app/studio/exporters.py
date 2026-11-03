"""TXT / DOCX / EPUB 导出：只用标准库，不引第三方排版依赖。

Markdown 与公众号 HTML 已经够「看一眼」，但交给编辑、印厂或阅读器时，
`.txt` / `.docx` / `.epub` 才是对方真能打开的东西。这里刻意不引
`python-docx` / `ebooklib`：两套格式本质都是 zip + XML，自己写能少两个
依赖，也避免版本升级时把导出链路带崩。

所有渲染函数都只吃 `_readable_sections()` 那一份结构：
`{title, content, parent_id, parent_title}`，按全书顺序排好。
"""
from __future__ import annotations

import io
import re
import uuid
import zipfile
from typing import Any
from xml.sax.saxutils import escape

__all__ = ["render_txt", "render_docx", "render_epub"]


def _paragraphs(text: str) -> list[str]:
    """把正文按空行切成段落；单换行在 Word/EPUB 里通常只是段内折行。"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _clean_title(work: dict[str, Any]) -> str:
    return (work.get("title") or "").strip() or "未命名作品"


def render_txt(work: dict[str, Any], sections: list[dict[str, Any]],
               author: str = "") -> str:
    """纯文本：分卷用空行隔开，章标题前留一空行，方便直接粘进任何编辑器。"""
    lines: list[str] = [_clean_title(work)]
    if author:
        lines.append(f"作者：{author}")
    last_parent = None
    for sec in sections:
        parent = sec.get("parent_id") or ""
        if parent and parent != last_parent:
            lines.extend(["", sec.get("parent_title") or ""])
            last_parent = parent
        lines.extend(["", sec.get("title") or ""])
        content = (sec.get("content") or "").strip()
        if content:
            lines.extend(["", content])
    return "\n".join(lines).rstrip() + "\n"


# ==================== DOCX ====================

_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

_DOCX_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_DOCX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体" w:hAnsi="Times New Roman"/><w:sz w:val="24"/></w:rPr></w:rPrDefault>
    <w:pPrDefault><w:pPr><w:spacing w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/><w:basedOn w:val="Normal"/>
    <w:pPr><w:jc w:val="center"/><w:spacing w:before="240" w:after="360"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="52"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
    <w:pPr><w:keepNext/><w:spacing w:before="360" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="36"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>
    <w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="1"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="30"/></w:rPr>
  </w:style>
</w:styles>"""


def _docx_run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _docx_p(text: str, style: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{_docx_run(text)}</w:p>"


def render_docx(work: dict[str, Any], sections: list[dict[str, Any]],
                author: str = "") -> bytes:
    """最小可用 DOCX：标题、分卷一级标题、章二级标题、正文段落。"""
    body: list[str] = [_docx_p(_clean_title(work), "Title")]
    if author:
        body.append(_docx_p(f"作者：{author}"))
    last_parent = None
    for sec in sections:
        parent = sec.get("parent_id") or ""
        if parent and parent != last_parent:
            body.append(_docx_p(sec.get("parent_title") or "", "Heading1"))
            last_parent = parent
        body.append(_docx_p(sec.get("title") or "", "Heading2"))
        for para in _paragraphs(sec.get("content") or ""):
            body.append(_docx_p(para))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(body)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
          '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
          'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )

    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(_clean_title(work))}</dc:title>"
        f"<dc:creator>{escape(author or '')}</dc:creator>"
        "</cp:coreProperties>"
    )
    app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        "<Application>Novel Studio</Application></Properties>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", _DOCX_STYLES)
        zf.writestr("word/_rels/document.xml.rels", _DOCX_DOC_RELS)
        zf.writestr("docProps/core.xml", core)
        zf.writestr("docProps/app.xml", app)
    return buf.getvalue()


# ==================== EPUB ====================

_EPUB_CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

_EPUB_CSS = """body { font-family: serif; line-height: 1.7; margin: 5%; }
h1, h2 { text-align: center; line-height: 1.4; }
h2 { margin-top: 2em; }
p { text-indent: 2em; margin: 0.6em 0; }
.title-page { text-align: center; margin-top: 30%; }
.title-page h1 { font-size: 2em; }
.meta { color: #666; }
"""


def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">'
        f"<head><title>{escape(title)}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>'
        f"<body>{body}</body></html>"
    )


def render_epub(work: dict[str, Any], sections: list[dict[str, Any]],
                author: str = "") -> bytes:
    """EPUB 3（同时带 NCX，老阅读器也认）：每节一个 XHTML，按卷分组进目录。"""
    title = _clean_title(work)
    book_id = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"novel-studio:{title}:{len(sections)}"))
    files: list[tuple[str, str]] = []          # (文件名, 标题)
    chapters: list[tuple[str, str, str]] = []  # (文件名, 标题, xhtml)
    nav_items: list[str] = []
    last_parent = None

    for i, sec in enumerate(sections, start=1):
        name = f"chapter-{i:04d}.xhtml"
        sec_title = sec.get("title") or f"第 {i} 节"
        parent = sec.get("parent_id") or ""
        parts: list[str] = []
        if parent and parent != last_parent:
            parts.append(f"<h1>{escape(sec.get('parent_title') or '')}</h1>")
            last_parent = parent
        parts.append(f"<h2>{escape(sec_title)}</h2>")
        for para in _paragraphs(sec.get("content") or ""):
            parts.append(f"<p>{escape(para)}</p>")
        chapters.append((name, sec_title, _xhtml(sec_title, "".join(parts))))
        files.append((name, sec_title))
        nav_items.append(f'<li><a href="{name}">{escape(sec_title)}</a></li>')

    title_page = _xhtml(title, (
        '<div class="title-page">'
        f"<h1>{escape(title)}</h1>"
        + (f'<p class="meta">{escape(author)}</p>' if author else "")
        + "</div>"
    ))

    nav = _xhtml("目录", (
        '<nav epub:type="toc" id="toc"><h1>目录</h1><ol>' + "".join(nav_items) + "</ol></nav>"
    ))

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="titlepage" href="title.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="titlepage"/>', '<itemref idref="nav"/>']
    for i, chapter in enumerate(chapters, start=1):
        name = chapter[0]
        manifest.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')

    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="bookid">{escape(book_id)}</dc:identifier>'
        f"<dc:title>{escape(title)}</dc:title>"
        f"<dc:language>zh-CN</dc:language>"
        + (f"<dc:creator>{escape(author)}</dc:creator>" if author else "")
        + '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>'
        "</metadata>"
        f'<manifest>{"".join(manifest)}</manifest>'
        f'<spine toc="ncx">{"".join(spine)}</spine>'
        "</package>"
    )

    nav_points = []
    for i, (name, sec_title) in enumerate(files, start=1):
        nav_points.append(
            f'<navPoint id="np{i}" playOrder="{i}">'
            f"<navLabel><text>{escape(sec_title)}</text></navLabel>"
            f'<content src="{name}"/></navPoint>'
        )
    ncx = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f'<head><meta name="dtb:uid" content="{escape(book_id)}"/></head>'
        f"<docTitle><text>{escape(title)}</text></docTitle>"
        f"<navMap>{''.join(nav_points)}</navMap></ncx>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # mimetype 必须是第一个条目且不压缩，阅读器靠这 20 个字节识别格式
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, "application/epub+zip")
        zf.writestr("META-INF/container.xml", _EPUB_CONTAINER, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", ncx, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", _EPUB_CSS, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/title.xhtml", title_page, zipfile.ZIP_DEFLATED)
        for name, _, page in chapters:
            zf.writestr(f"OEBPS/{name}", page, zipfile.ZIP_DEFLATED)
    return buf.getvalue()
