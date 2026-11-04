"""交付格式：TXT / DOCX / EPUB 都是「点一下能打开」的成稿，不是 Markdown 换皮。

这三个格式故意不引 `python-docx` / `ebooklib`：DOCX 与 EPUB 本质都是
zip + XML，用标准库写能少两个依赖，也方便在测试里直接拆包验证——
只看 HTTP 200 不够，压缩包里少了 `[Content_Types].xml` 或 mimetype
被压缩，Word / 阅读器照样打不开。
"""
from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import StudioStore


def make_app(store: StudioStore) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.include_router(studio_routes.router)
    return app


def seed(client: TestClient) -> str:
    work = client.post("/studio/works", json={"kind": "novel", "title": "交付测试",
                                              "scale": "series"}).json()
    vol = client.post(f"/studio/works/{work['id']}/nodes",
                      json={"kind": "volume", "title": "第一卷"}).json()
    client.post(f"/studio/works/{work['id']}/nodes",
                json={"kind": "chapter", "title": "第一章", "parent_id": vol["id"],
                      "content": "灯还亮着。\n\n她把门关上。"})
    client.post(f"/studio/works/{work['id']}/nodes",
                json={"kind": "chapter", "title": "第二章", "parent_id": vol["id"],
                      "content": "风从北边来。"})
    return work["id"]


def test_txt_export_has_reading_order_and_author(tmp_path) -> None:
    store = StudioStore(tmp_path / "studio.db")
    with TestClient(make_app(store)) as client:
        wid = seed(client)
        r = client.get(f"/studio/works/{wid}/export?format=txt&author=阿泠")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-sections"] == "2"
    body = r.content.decode("utf-8")
    assert body.startswith("交付测试\n作者：阿泠\n")
    assert body.index("第一卷") < body.index("第一章") < body.index("灯还亮着。")
    assert body.index("第一章") < body.index("第二章")
    assert "她把门关上。" in body


def test_docx_is_a_real_word_package(tmp_path) -> None:
    store = StudioStore(tmp_path / "studio.db")
    with TestClient(make_app(store)) as client:
        wid = seed(client)
        r = client.get(f"/studio/works/{wid}/export?format=docx&author=阿泠")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
            "word/styles.xml", "docProps/core.xml"} <= names

    document = zf.read("word/document.xml").decode("utf-8")
    assert "交付测试" in document and "第一章" in document
    assert "灯还亮着。" in document
    assert "Heading2" in document, "章标题要带大纲级别，Word 导航窗格才认"
    core = zf.read("docProps/core.xml").decode("utf-8")
    assert "<dc:creator>阿泠</dc:creator>" in core


def test_epub_has_valid_mimetype_and_spine(tmp_path) -> None:
    store = StudioStore(tmp_path / "studio.db")
    with TestClient(make_app(store)) as client:
        wid = seed(client)
        r = client.get(f"/studio/works/{wid}/export?format=epub&author=阿泠")

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/epub+zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    first = zf.infolist()[0]
    assert first.filename == "mimetype"
    assert first.compress_type == zipfile.ZIP_STORED, "mimetype 被压缩，阅读器会认不出"
    assert zf.read("mimetype") == b"application/epub+zip"

    names = set(zf.namelist())
    assert {"META-INF/container.xml", "OEBPS/content.opf", "OEBPS/nav.xhtml",
            "OEBPS/title.xhtml", "OEBPS/style.css"} <= names
    opf = zf.read("OEBPS/content.opf").decode("utf-8")
    assert "<dc:title>交付测试</dc:title>" in opf
    assert "<dc:creator>阿泠</dc:creator>" in opf
    assert 'properties="nav"' in opf
    chapter = zf.read("OEBPS/chapter-0001.xhtml").decode("utf-8")
    assert "第一章" in chapter and "灯还亮着。" in chapter
    nav = zf.read("OEBPS/nav.xhtml").decode("utf-8")
    assert "第一章" in nav and "第二章" in nav


def test_export_without_content_is_refused(tmp_path) -> None:
    store = StudioStore(tmp_path / "studio.db")
    with TestClient(make_app(store)) as client:
        work = client.post("/studio/works", json={"kind": "novel", "title": "空书",
                                                  "scale": "series"}).json()
        r = client.get(f"/studio/works/{work['id']}/export?format=docx")

    assert r.status_code == 400
    assert "还没有可导出的内容" in r.json()["detail"]
