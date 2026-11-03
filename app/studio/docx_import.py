"""DOCX 导入：把 Word 的 .docx 解成纯文本，交回前端既有的拆节流程。

只用标准库（zipfile + ElementTree），与 `exporters.py` 写 DOCX 同一取向：
不引入 python-docx。浏览器端也不碰二进制——前端把 .docx 原样 POST 过来，
后端解完把文本塞进导入弹层的草稿框，预览、拆法选择、逐节落库全部复用
`ImportDocDialog` 已有的那一条路：用户点「导入」之前一个字都不写库。

标题识别是尽力而为：`w:pStyle` 的 val 常见三种写法——英文文档的
`Heading1`、中文文档的 `1`（styleId 就是数字）、以及 `heading 1`。
认出来的加上 markdown 井号前缀，正好接上前端 `HEAD_RE` 的「## 标题」分支；
认不出来的原样留行，用户还可以手动换「按空行拆 / 整篇算一节」。
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 上传上限：正文型 docx 一般几百 KB，20MB 以外多半是塞了高清图，不是稿子
MAX_DOCX_BYTES = 20 * 1024 * 1024
# 解压上限：挡 zip bomb——document.xml 本身是纯 XML，正常稿子到不了 64MB
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024

# pStyle 认标题：Heading1 / heading 1 / 标题1 / 纯数字 styleId（中文版 Word 的「标题 1」）
_HEADING_RE = re.compile(r"^(?:heading\s*([1-6])|标题\s*([1-6])|([1-6]))$", re.IGNORECASE)


def _heading_level(style_val: str) -> int:
    m = _HEADING_RE.match((style_val or "").strip())
    if not m:
        return 0
    for g in m.groups():
        if g:
            return int(g)
    return 0


def _para_text(p: ET.Element) -> str:
    """一个段落的纯文本：w:t 取值、w:tab 记制表符、换行记回车。

    只走直接含的 run 不够——超链接（w:hyperlink）里也套 w:r——所以整段 iter。
    批注（w:commentReference）与脚注引用不带正文，天然被跳过。
    """
    parts: list[str] = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{_W}t":
            parts.append(node.text or "")
        elif tag == f"{_W}tab":
            parts.append("\t")
        elif tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _para_style(p: ET.Element) -> str:
    ppr = p.find(f"{_W}pPr")
    if ppr is None:
        return ""
    st = ppr.find(f"{_W}pStyle")
    return st.get(f"{_W}val", "") if st is not None else ""


def docx_to_text(data: bytes) -> str:
    """.docx 字节 → 纯文本（每段一行；空段保留为空行，标题带 markdown 井号）。

    不是 zip、没有 document.xml、超大，都抛 ValueError——路由层按 400 报出去，
    消息直接当界面提示用，所以要说人话。
    """
    if not data:
        raise ValueError("文件是空的")
    if len(data) > MAX_DOCX_BYTES:
        raise ValueError("文件超过 20 MB：多半塞的是图片而不是稿子，请删掉图片再导")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("这不是有效的 .docx（老 .doc 格式请先在 Word 里另存为 .docx）") from None
    try:
        info = zf.getinfo("word/document.xml")
        if info.file_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("文档解压后过大，拒绝解析")
        xml = zf.read("word/document.xml")
    except KeyError:
        raise ValueError("这个 .docx 里没有正文文件（word/document.xml 缺失）") from None
    finally:
        zf.close()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("docx 的正文读不懂，文件可能已损坏") from exc

    body = root.find(f"{_W}body")
    if body is None:
        return ""
    lines: list[str] = []
    for p in body.iter(f"{_W}p"):
        line = _para_text(p).strip()
        level = _heading_level(_para_style(p))
        if line and level:
            line = f"{'#' * level} {line}"
        lines.append(line)
    # 连续空行压成一行了：留一个空行就够前端按空行拆
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()
