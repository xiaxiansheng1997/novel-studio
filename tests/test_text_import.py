"""文本 / Markdown / EPUB 导入的回归测试。

口径与 `docx_import` 对称：只用标准库、只解正文、坏文件说人话、路由统一 400。
EPUB 这边最值得测的不是「能不能解」，而是三件容易翻车的事：
按 spine 顺序而不是 manifest 书写顺序、`&nbsp;` 这类只在外部 DTD 里声明的
实体不能炸掉解析、目录页（nav）不能混进正文当成一节。
文本类那边核心就一条：GBK 中文稿必须解对并且如实报出编码。
"""
from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import text_import
from app.studio.store import StudioStore
from app.studio.text_import import (
    detect_text_encoding,
    epub_to_text,
    parse_document,
    txt_to_text,
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh">
<head><title>第一章</title><style>body { color: red; }</style></head>
<body>
<h1>第一章&nbsp;灯下</h1>
<p>她推开门，<span>灯还亮着</span>。</p>
<p>第二段：带实体 &amp; 符号，还有 &ldquo;引号&rdquo;。</p>
<!-- 这句注释不该出现在正文里 -->
<div class="note"><p>div 里的段落也要能读出来</p></div>
</body>
</html>
"""

CH2 = """<html xmlns="http://www.w3.org/1999/xhtml"><head><title>2</title></head>
<body><h2>小节</h2><p>雨在窗外下。</p></body></html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>夜航船</dc:title>
  <dc:language>zh</dc:language>
 </metadata>
 <manifest>
  <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine toc="ncx">
  <itemref idref="nav"/>
  <itemref idref="c1"/>
  <itemref idref="c2"/>
 </spine>
</package>
"""

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
 <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def make_epub(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER)
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def standard_epub() -> bytes:
    return make_epub({
        "OEBPS/content.opf": OPF,
        "OEBPS/text/ch1.xhtml": CH1,
        "OEBPS/text/ch2.xhtml": CH2,
        "OEBPS/nav.xhtml": '<html><body><h1>目录</h1><p>链接文字不算正文</p></body></html>',
        "OEBPS/toc.ncx": '<ncx><navMap><navLabel><text>第一章</text></navLabel></navMap></ncx>',
    })


def make_docx(paragraph_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{_W}"><w:body>{paragraph_xml}</w:body></w:document>')
    return buf.getvalue()


# ==================== EPUB ====================

def test_epub_follows_spine_not_manifest_order() -> None:
    """manifest 里 c2 写在 c1 前面，spine 说 c1 先：正文必须按阅读顺序。"""
    text, title = epub_to_text(standard_epub())
    assert title == "夜航船"
    assert text.index("第一章") < text.index("雨在窗外下"), "顺序错了会把书拆成颠倒的稿子"


def test_epub_headings_keep_markdown_level() -> None:
    """h1→#、h2→##：前端「按标题拆」与导出都靠这一层井号。"""
    text, _ = epub_to_text(standard_epub())
    assert "# 第一章 灯下" in text
    assert "## 小节" in text
    assert text.splitlines()[0].startswith("# "), "书名补不进一级标题时至少首行是标题"


def test_epub_entities_and_nested_tags_survive() -> None:
    """&nbsp; / &ldquo; 这类外部 DTD 实体不能炸解析；嵌套 span 只剥标签不丢字。"""
    text, _ = epub_to_text(standard_epub())
    assert "她推开门，灯还亮着。" in text
    assert "第二段：带实体 & 符号，还有 “引号”。" in text


def test_epub_drops_head_style_and_comments() -> None:
    text, _ = epub_to_text(standard_epub())
    assert "color: red" not in text
    assert "不该出现在正文里" not in text
    assert "div 里的段落也要能读出来" in text


def test_epub_skips_nav_page_and_non_xhtml() -> None:
    """properties="nav" 的目录页与 ncx 都不算正文，否则第一節是一堆链接。"""
    text, _ = epub_to_text(standard_epub())
    assert "链接文字不算正文" not in text
    assert "第一章</text" not in text


def test_epub_title_prepended_when_no_heading() -> None:
    """整本没有标题行时补上书名：至少给「按标题拆」一个锚点。"""
    single_opf = """<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
     <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>无题集</dc:title></metadata>
     <manifest><item id="only" href="o.xhtml" media-type="application/xhtml+xml"/></manifest>
     <spine><itemref idref="only"/></spine>
    </package>"""
    epub = make_epub({
        "OEBPS/content.opf": single_opf,
        "OEBPS/o.xhtml": "<html><body><p>只有一段话。</p></body></html>",
    })
    text, title = epub_to_text(epub)
    assert title == "无题集"
    assert text.startswith("# 无题集\n")


def test_epub_rejects_bad_files() -> None:
    with pytest.raises(ValueError, match="空的"):
        epub_to_text(b"")
    with pytest.raises(ValueError, match="有效的 .epub"):
        epub_to_text(b"definitely not a zip")
    # 是 zip 但没有 container.xml
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
    with pytest.raises(ValueError, match="container.xml"):
        epub_to_text(buf.getvalue())
    # 清单里写了章节但文件全都缺失：跳完没东西可读，也要说人话
    missing = make_epub({
        "OEBPS/content.opf": OPF,
        "OEBPS/nav.xhtml": CH2,
        "OEBPS/toc.ncx": "<ncx/>",
    })
    with pytest.raises(ValueError, match="没有可读的正文"):
        epub_to_text(missing)


def test_epub_huge_item_refused(monkeypatch) -> None:
    monkeypatch.setattr(text_import, "MAX_EPUB_ITEM_BYTES", 16)
    with pytest.raises(ValueError, match="过大"):
        epub_to_text(standard_epub())


# ==================== 文本编码 ====================

def test_gbk_text_is_detected_not_mangled() -> None:
    """这条是本次改动的动机：浏览器 file.text() 会把 GBK 中文读成乱码。"""
    raw = "第一章 灯下\r\n她推开门，雨还在下。\r\n".encode("gbk")
    text = txt_to_text(raw)
    assert "她推开门，雨还在下。" in text
    assert detect_text_encoding(raw) == "GB18030"
    assert "\r" not in text, "CRLF 要归一成 \\n，前端按空行拆才准"


def test_utf8_bom_and_utf16_round_trip() -> None:
    bom = "标题\n正文".encode("utf-8")
    assert txt_to_text(b"\xef\xbb\xbf" + bom) == "标题\n正文"
    assert detect_text_encoding(b"\xef\xbb\xbf" + bom) == "UTF-8 (BOM)"
    u16 = "夜里行船".encode("utf-16")
    assert txt_to_text(u16) == "夜里行船"
    assert detect_text_encoding(u16) == "UTF-16"


def test_plain_utf8_wins_over_gb18030() -> None:
    """顺序不能反：GB18030 几乎对任意字节都不报错，先试它会把 UTF-8 解成乱码。"""
    raw = "三段文字\n第二行".encode("utf-8")
    text, enc = text_import._decode_bytes(raw)  # noqa: SLF001 - 就是测这个内部口径
    assert enc == "UTF-8"
    assert text == "三段文字\n第二行"


def test_binary_and_empty_refused() -> None:
    with pytest.raises(ValueError, match="空的"):
        txt_to_text(b"")
    with pytest.raises(ValueError, match="二进制"):
        txt_to_text(b"\xd0\xcf\x11\xe0\x00\x00old doc")
    with pytest.raises(ValueError, match="PDF"):
        parse_document("稿子.pdf", b"%PDF-1.4 something")


def test_blank_lines_collapsed_and_nbsp_cleaned() -> None:
    raw = "第一段   \n\n\n\n第二段\xa0结束\n\x0c"
    assert txt_to_text(raw.encode("utf-8")) == "第一段\n\n第二段 结束"


def test_text_size_cap(monkeypatch) -> None:
    monkeypatch.setattr(text_import, "MAX_TEXT_BYTES", 8)
    with pytest.raises(ValueError, match="8 MB"):
        parse_document("big.txt", "很长的稿子".encode("utf-8") * 10)


# ==================== 分派与嗅探 ====================

def test_parse_document_dispatches_by_extension() -> None:
    out = parse_document("稿子.epub", standard_epub())
    assert out["kind"] == "epub" and out["title"] == "夜航船"
    assert out["headings"] == 2 and out["chars"] == len(out["text"])

    docx = make_docx('<w:p><w:r><w:t>纯文本一份</w:t></w:r></w:p>')
    out2 = parse_document("word.docx", docx)
    assert out2["kind"] == "docx" and "纯文本一份" in out2["text"]
    assert out2["encoding"] == "", "编码只对文本类有意义"

    out3 = parse_document("note.md", "# 标题\n\n正文".encode("utf-8"))
    assert out3["kind"] == "md" and out3["headings"] == 1


def test_parse_document_sniffs_when_name_missing() -> None:
    """没文件名也要能判：zip 里有 mimetype 就是 epub，有 document.xml 就是 docx。"""
    assert parse_document("", standard_epub())["kind"] == "epub"
    docx = make_docx('<w:p><w:r><w:t>x</w:t></w:r></w:p>')
    assert parse_document("mystery", docx)["kind"] == "docx"
    assert parse_document("odd.bin", "一段中文".encode("gbk"))["encoding"] == "GB18030"


# ==================== 接口层 ====================

def _build_client(tmp_path):
    store = StudioStore(tmp_path / "studio.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.include_router(studio_routes.router)
    return TestClient(app)


def test_import_doc_route(tmp_path) -> None:
    """/studio/import/doc：文件名走 query，回一份能直接摆上界面的摘要。"""
    with _build_client(tmp_path) as client:
        r = client.post("/studio/import/doc?name=夜航船.epub",
                        content=standard_epub(),
                        headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "epub" and body["title"] == "夜航船"
        assert "# 第一章 灯下" in body["text"]
        assert body["chars"] == len(body["text"])

        gbk = client.post("/studio/import/doc?name=稿子.txt",
                          content="第一章 灯下\r\n正文。".encode("gbk"))
        assert gbk.status_code == 200
        assert gbk.json()["encoding"] == "GB18030"
        assert "正文。" in gbk.json()["text"]

        bad = client.post("/studio/import/doc?name=x.epub", content=b"garbage")
        assert bad.status_code == 400
        assert "epub" in bad.json()["detail"]

        empty = client.post("/studio/import/doc?name=x.txt", content=b"   \n  ")
        assert empty.status_code == 400
        assert "没有可读的文字" in empty.json()["detail"]
