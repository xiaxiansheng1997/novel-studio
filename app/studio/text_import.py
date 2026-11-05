"""纯文本 / Markdown / EPUB 导入：与 `docx_import.py` 同一取向，只用标准库。

为什么要多这一个口子：拆解（craft）能吃进几百章，可导入这边原来只认真正的
`.txt`/`.md` 由浏览器按 UTF-8 读——Windows 上别人发你的小说文本十有八九是
GBK/GB2312，浏览器硬读出来的是一堆「鎷栨嫿」式乱码，字还不报错，等导进库里
才发现整本书废了。所以文本类也走后端：先探编码，再归一化换行。

EPUB 则是「有胃口没嘴」的另一半：一本合法 EPUB 就是 zip + XHTML，正文按
spine（阅读顺序）串起来，标题样式在 `<h1>~<h6>` 里，转成 markdown 井号后
正好接上前端 `ImportDocDialog` 已有的「按标题拆」那一条路。

XHTML 不实用 XML 解析器：EPUB 里的 XHTML 常带 `&nbsp;` 这类只在外部 DTD 里
声明过的实体，ElementTree 会直接 ParseError——而网上下载的书写得再不规范
也得能读。这里按标签文本抽取（先摘标题、再剥标签、最后 unescape），宁可
丢掉一点结构，也不要整本读不出来。
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from urllib.parse import unquote

from app.studio.docx_import import docx_to_text

# 文本类上限：8MB 的 txt 约等于一千多万字，多半是选错了文件
MAX_TEXT_BYTES = 8 * 1024 * 1024
# EPUB 带图动辄十几 MB，给到 60MB；单章解压上限防 zip bomb
MAX_EPUB_BYTES = 60 * 1024 * 1024
MAX_EPUB_ITEM_BYTES = 8 * 1024 * 1024

TEXT_EXTS = (".txt", ".md", ".markdown", ".text", ".mdown", ".mkd")

_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# head/script/style 整块丢掉：前者是元数据，后两者在正文里只会污染字
_DROP_BLOCK_RE = re.compile(
    r"<(head|script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.S | re.I)
# 这些标签的收尾处算换行：EPUB 的段落/列表/标题边界基本都靠它们
_BLOCK_END_RE = re.compile(
    r"</(?:p|div|li|ul|ol|blockquote|section|article|body|tr|h[1-6])\s*>",
    re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RE = re.compile(r"\n{3,}")


def _decode_bytes(data: bytes) -> tuple[str, str]:
    """字节 → (文本, 编码名)：先看 BOM，再 UTF-8 严格，再 GB18030。

    顺序是有讲究的：GBK 的字节序列极少能凑成合法 UTF-8，所以「UTF-8 先试」
    不会把中文 GBK 文件误判成 UTF-8；反过来先试 GB18030 会把所有 UTF-8
    中文都解成乱码（它几乎对任意字节都不报错）。
    """
    if not data:
        raise ValueError("文件是空的")
    # UTF-16 本身带零字节，只有非 BOM 开头却含 NUL 才当二进制拦下来：
    # 老 .doc 也是 zip 以外的二进制，不拦就会把一坨乱码当成正文导进库
    if b"\x00" in data[:4096] and data[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        raise ValueError("这个文件看着是二进制而不是文本（老 .doc 请先在 Word 里另存为 .docx）")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace"), "UTF-8 (BOM)"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), "UTF-16"
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        pass
    for name in ("gb18030", "big5"):
        try:
            return data.decode(name), name.upper()
        except UnicodeDecodeError:
            continue
    # 全试穿：按 UTF-8 硬解并标记出来，让界面如实说「编码不确定」，
    # 而不是悄悄把乱码当成正文写进库
    return data.decode("utf-8", errors="replace"), "unknown"


def _normalize(text: str) -> str:
    """换行与空白归一：\r\n → \n，翻页符/零宽 BOM 清掉，连续空行压成一行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufeff", "").replace("\x0c", "\n").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


def txt_to_text(data: bytes) -> str:
    """txt/md 字节 → 纯文本（探编码后归一化）。"""
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("文本超过 8 MB：多半选错了文件，请确认这是稿子")
    text, _ = _decode_bytes(data)
    return _normalize(text)


def detect_text_encoding(data: bytes) -> str:
    """只报编码名：给界面一句实话（「按 GB18030 解的」），不做别的判断。"""
    _, name = _decode_bytes(data)
    return name


def _strip_tags(raw: str) -> str:
    """只剥标签，不解实体：解实体统一放在最后一趟，避免双解（&amp;amp; 会被多解一层）。"""
    return _TAG_RE.sub("", raw)


def _plain(raw: str) -> str:
    """剥标签并解实体：给 OPF 里的书名这类独立短串用。"""
    return html.unescape(_strip_tags(raw))


def _html_body_to_lines(markup: str) -> list[str]:
    """一章 XHTML → 若干行文本：标题带 markdown 井号，段落之间空一行。"""
    body = _COMMENT_RE.sub("", _DROP_BLOCK_RE.sub("", markup))
    # 先摘标题：井号级数照 h 标签来（h1→#，h2→##），内部残留标签剥掉、实体留到最后统一解
    body = _HEADING_RE.sub(
        lambda m: f"\n{'#' * int(m.group(1))} {_strip_tags(m.group(2)).strip()}\n",
        body)
    body = _BR_RE.sub("\n", _BLOCK_END_RE.sub("\n", body))
    return _normalize(html.unescape(_TAG_RE.sub("", body))).split("\n")


def _opf_items(opf: str) -> tuple[list[str], dict[str, str], dict[str, str], str]:
    """从 OPF 里取（spine 顺序的 itemid, id→href, id→properties, 书名）。

    用正则而不是 XML 解析：OPF 本身一般没问题，但属性取名这两步用不上树，
    少一个会因外部实体炸掉的环节。
    """
    manifest: dict[str, str] = {}
    props: dict[str, str] = {}
    for tag in re.finditer(r"<item\b([^>]*)/?>", opf, re.I):
        attrs = dict(_ATTR_RE.findall(tag.group(1)))
        if not attrs.get("id"):
            continue
        manifest[attrs["id"]] = attrs.get("href", "")
        props[attrs["id"]] = attrs.get("properties", "")
    spine_tag = re.search(r"<spine\b([^>]*)>(.*?)</spine>", opf, re.S | re.I)
    order: list[str] = []
    if spine_tag:
        for ref in re.finditer(r"<itemref\b([^>]*)/?>", spine_tag.group(2), re.I):
            attrs = dict(_ATTR_RE.findall(ref.group(1)))
            idref = attrs.get("idref", "")
            # linear="no" 是导航页一类：读正文不该混进来
            if idref and attrs.get("linear", "yes").lower() != "no":
                order.append(idref)
    if not order:  # 没有 spine（极少见但存在）：退到 manifest 的书写顺序
        order = list(manifest)
    title = ""
    m = re.search(r"<dc:title\b[^>]*>(.*?)</dc:title\s*>", opf, re.S | re.I)
    if m:
        title = _plain(m.group(1)).strip()
    else:
        m = re.search(r"<metadata\b[^>]*>(.*?)</metadata\s*>", opf, re.S | re.I)
        if m:
            t = re.search(r"<title\b[^>]*>(.*?)</title\s*>", m.group(1), re.S | re.I)
            title = _plain(t.group(1)).strip() if t else ""
    return order, manifest, props, title


def _resolve(base_dir: str, href: str) -> str:
    """OPF 里的相对 href → zip 内的条目名（epub 用正斜杠，不做 URL 解码会漏空格名）。"""
    path = unquote(href or "").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path.startswith("/"):
        return path[1:]
    return f"{base_dir}/{path}" if base_dir else path


def epub_to_text(data: bytes) -> tuple[str, str]:
    """.epub 字节 → (纯文本, 书名)。

    不是 zip、缺 container.xml / OPF、没有任何 XHTML，都抛 ValueError；
    消息直接当界面提示用，所以要说人话并且给出下一步。
    """
    if not data:
        raise ValueError("文件是空的")
    if len(data) > MAX_EPUB_BYTES:
        raise ValueError("EPUB 超过 60 MB：多半塞的是高清图片，请用工具压一遍再导")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("这不是有效的 .epub（epub 本质是 zip，请先确认文件没坏）") from None
    try:
        names = set(zf.namelist())
        try:
            raw = zf.read("META-INF/container.xml")
        except KeyError:
            raise ValueError(
                "这个 EPUB 里没有 META-INF/container.xml（文件不完整，可能下载断了）") from None
        container = raw.decode("utf-8", errors="replace")
        m = re.search(r'full-path\s*=\s*"([^"]+)"', container)
        if not m:
            raise ValueError("EPUB 的 META-INF/container.xml 里没写出内容文件位置")
        opf_name = unquote(m.group(1))
        if opf_name not in names:
            # 少数书把 full-path 写成带转义或大小写不一致的形式：按不区分大小写再找一次
            lowered = {n.lower(): n for n in names}
            if opf_name.lower() in lowered:
                opf_name = lowered[opf_name.lower()]
            else:
                raise ValueError(f"EPUB 里找不到内容清单文件 {opf_name}")
        opf = zf.read(opf_name).decode("utf-8", errors="replace")
        order, manifest, props, title = _opf_items(opf)
        base_dir = opf_name.rsplit("/", 1)[0] if "/" in opf_name else ""
        lowered = {n.lower(): n for n in names}

        lines: list[str] = []
        used = 0
        for item_id in order:
            href = manifest.get(item_id, "")
            if not href:
                continue
            # properties="nav" 是目录页（EPUB3）：混进正文会变成一节只有链接的干扁内容
            if "nav" in props.get(item_id, "").split():
                continue
            entry = _resolve(base_dir, href)
            if entry not in names:
                if entry.lower() not in lowered:
                    continue   # 清单里写了但文件缺失：跳过这一章，别把整本判死
                entry = lowered[entry.lower()]
            if not re.search(r"\.(x?html?)$", entry, re.I):
                continue
            info = zf.getinfo(entry)
            if info.file_size > MAX_EPUB_ITEM_BYTES:
                raise ValueError(f"章节 {entry} 解压后过大，拒绝解析")
            used += info.file_size
            if used > 128 * 1024 * 1024:
                raise ValueError("EPUB 解压后总大小超过 128 MB，拒绝解析")
            markup = zf.read(entry).decode("utf-8", errors="replace")
            chunk = _html_body_to_lines(markup)
            if chunk:
                if lines and lines[-1]:
                    lines.append("")
                lines.extend(chunk)
        if not lines:
            raise ValueError("这个 EPUB 里没有可读的正文（章节文件都不是 XHTML？）")
        body = _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()
        if title and not body.startswith("#"):
            # 书名补成一级标题：整本只有一章时，前端「按标题拆」至少有个锚点
            body = f"# {title}\n\n{body}"
        return body, title
    finally:
        zf.close()


def _sniff(data: bytes) -> str:
    """没有文件名时按内容判类型：zip 里有没有 mimetype / document.xml。"""
    if data.startswith(b"%PDF"):
        raise ValueError("PDF 读不了：请用 Calibre 或 Word 另存为 .epub / .docx / 纯文本")
    if data[:2] == b"PK":
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except zipfile.BadZipFile:
            names = set()
        if "mimetype" in names:
            return ".epub"
        if "word/document.xml" in names:
            return ".docx"
    return ".txt"


def parse_document(name: str, data: bytes) -> dict[str, object]:
    """按扩展名分派解析，认不出来就嗅探内容；返回前端一份可直接摆的摘要。

    title 只有 epub 给得出（docx/txt 没有元数据）；encoding 只有文本类给，
    好让界面上那句「按 GB18030 解的」是真话而不是猜。
    """
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in (".docx", ".epub") and ext not in TEXT_EXTS:
        ext = _sniff(data)

    title = ""
    encoding = ""
    if ext == ".docx":
        text = docx_to_text(data)
    elif ext == ".epub":
        text, title = epub_to_text(data)
    else:
        if len(data) > MAX_TEXT_BYTES:
            raise ValueError("文本超过 8 MB：多半选错了文件，请确认这是稿子")
        text, encoding = _decode_bytes(data)
        text = _normalize(text)

    if not text:
        raise ValueError("这个文件里没有可读的文字")
    return {
        "text": text,
        "chars": len(text),
        # 带井号的标题行数：前端拿来提示「按标题拆能不能拆得开」
        "headings": sum(1 for line in text.splitlines() if line.startswith("#")),
        "kind": ext.lstrip("."),
        "title": title,
        "encoding": encoding,
    }
