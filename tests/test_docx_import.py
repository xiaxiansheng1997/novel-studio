"""DOCX 导入：标准库解 zip+document.xml 的回归测试。

口径与导出对称（`exporters.py` 用标准库写 DOCX，这里用标准库读）：
只解正文——标题样式（Heading1 / 中文版 Word 的数字 styleId）加上 markdown
井号前缀，正好接上前端 ImportDocDialog 的「按标题拆」；坏文件说人话，
路由层统一 400。这一步不写库：解出来的文本回到弹层预览，落库仍走
`create_node` 老路，点「导入」之前一个字都不进。
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
from app.studio.docx_import import docx_to_text
from app.studio.store import StudioStore

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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


def p(text: str = "", style: str = "", extra: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    run = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f"<w:p>{ppr}{run}{extra}</w:p>"


# ==================== 解析 ====================

def test_paragraphs_and_headings_become_plain_text() -> None:
    docx = make_docx(
        p("第一章 灯下", style="Heading1")
        + p("她推开门，", extra='<w:r><w:t>灯还亮着。</w:t></w:r>')  # 多 run 拼一段
        + p()                                                        # 空段留空行
        + p("雨在窗外下。")
        + p("", extra='<w:r><w:t>带</w:t></w:r><w:r><w:tab/><w:t>制表</w:t></w:r>')
    )

    text = docx_to_text(docx)

    lines = text.splitlines()
    assert lines[0] == "# 第一章 灯下", "Heading1 → markdown 井号，接前端「## 标题」拆法"
    assert "她推开门，灯还亮着。" in text, "同段多 run 要拼成一行"
    assert "带\t制表" in text
    assert "\n\n雨在窗外下。" in text, "空段保留成一个空行"


def test_chinese_word_numeric_style_id_is_a_heading() -> None:
    # 中文版 Word 的「标题 1」styleId 就是 "1"；「标题 2」是 "2"
    docx = make_docx(p("第二章 雨中", style="1") + p("正文。") + p("小节", style="2"))
    text = docx_to_text(docx)
    assert "# 第二章 雨中" in text
    assert "## 小节" in text
    assert "\n正文。\n" in f"\n{text}\n", "普通段不加井号"


def test_hyperlink_text_is_kept_and_degenerate_files_refused() -> None:
    docx = make_docx(
        p("参考", extra='<w:hyperlink><w:r><w:t>这条线</w:t></w:r></w:hyperlink>'))
    assert docx_to_text(docx) == "参考这条线"

    with pytest.raises(ValueError, match="空的"):
        docx_to_text(b"")
    with pytest.raises(ValueError, match="有效的 .docx"):
        docx_to_text(b"not a zip at all, really")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")   # 没有 word/document.xml
    with pytest.raises(ValueError, match="没有正文文件"):
        docx_to_text(buf.getvalue())
    with pytest.raises(ValueError, match="损坏"):
        docx_to_text(make_docx("<w:p>unclosed"))


# ==================== 与自家导出 round-trip ====================

def test_round_trip_with_exporters_render_docx() -> None:
    """exporters 写出的 docx 要能被这里读回：导出再导入不该丢正文。"""
    from app.studio.exporters import render_docx

    work = {"title": "往返", "kind": "article"}
    sections = [
        {"title": "第一节", "content": "灯下写了些什么。\n\n第二段。"},
        {"title": "第二节", "content": "雨中的事。"},
    ]
    text = docx_to_text(render_docx(work, sections))
    assert "灯下写了些什么。" in text
    assert "第二段。" in text
    assert "雨中的事。" in text
    assert "第一节" in text and "第二节" in text


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


def test_import_docx_route(tmp_path) -> None:
    """/studio/import/docx：收原始字节，回文本与标题数；坏文件 400 带人话。"""
    with _build_client(tmp_path) as client:
        docx = make_docx(p("第一章 灯下", style="Heading1") + p("正文一行。"))
        r = client.post("/studio/import/docx", content=docx,
                        headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200
        body = r.json()
        assert body["headings"] == 1
        assert "# 第一章 灯下" in body["text"] and "正文一行。" in body["text"]
        assert body["chars"] == len(body["text"])

        assert client.post("/studio/import/docx", content=b"garbage").status_code == 400
        empty = make_docx(p("   "))   # 只有空白段：没有可读文字
        assert client.post("/studio/import/docx", content=empty).status_code == 400
