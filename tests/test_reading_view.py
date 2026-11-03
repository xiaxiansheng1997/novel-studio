"""迭代 C：整本阅读模式的数据端点。

阅读页要画目录与逐节渲染，所以给的是**列表**而不是拼好的 markdown 字符串
（拆字符串反推结构最容易漂）。与导出/发布检查同源：_readable_sections。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import StudioStore


@pytest.fixture
def client(tmp_path):
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
    with TestClient(app) as c:
        yield c


def _seed(client) -> str:
    """两卷三节 + 一节空正文：顺序与「空节默认不给」一次布好。"""
    work = client.post("/studio/works", json={"kind": "novel", "title": "通读",
                                              "scale": "series"}).json()
    v1 = client.post(f"/studio/works/{work['id']}/nodes",
                     json={"kind": "volume", "title": "第一卷"}).json()
    v2 = client.post(f"/studio/works/{work['id']}/nodes",
                     json={"kind": "volume", "title": "第二卷"}).json()
    client.post(f"/studio/works/{work['id']}/nodes",
                json={"kind": "chapter", "title": "第1章", "parent_id": v2["id"],
                      "content": "后建的卷里的字"})
    client.post(f"/studio/works/{work['id']}/nodes",
                json={"kind": "chapter", "title": "第2章", "parent_id": v1["id"],
                      "content": "第一段\n\n第二段"})
    client.post(f"/studio/works/{work['id']}/nodes",
                json={"kind": "chapter", "title": "第3章", "parent_id": v1["id"],
                      "content": "   "})
    return work["id"]


def test_reading_returns_ordered_sections_with_parent(client):
    wid = _seed(client)
    data = client.get(f"/studio/works/{wid}/reading").json()

    assert data["title"] == "通读" and data["kind"] == "novel"
    titles = [s["title"] for s in data["sections"]]
    # 按全序而不是按建节点的顺序：第1章在第二卷、先建，仍排第一
    assert titles == ["第1章", "第2章"]
    parents = [s["parent_title"] for s in data["sections"]]
    assert parents == ["第二卷", "第一卷"]
    assert data["chars"] == len("后建的卷里的字") + len("第一段\n\n第二段")
    # 空正文的节默认不给：阅读页不该出现只有标题的窟窿
    assert all(s["content"].strip() for s in data["sections"])


def test_reading_include_empty(client):
    wid = _seed(client)
    data = client.get(f"/studio/works/{wid}/reading", params={"include_empty": True}).json()
    assert [s["title"] for s in data["sections"]] == ["第1章", "第2章", "第3章"]


def test_reading_same_source_as_export(client):
    """与 assemble（导出）同源：阅读页的顺序和内容不许和导出的对不上。"""
    wid = _seed(client)
    reading = client.get(f"/studio/works/{wid}/reading").json()
    doc = client.post(f"/studio/works/{wid}/assemble").json()
    for sec in reading["sections"]:
        assert f"### {sec['title']}" in doc["markdown"]
        assert doc["markdown"].index(f"### {sec['title']}") < doc["markdown"].index(sec["content"])


def test_reading_unknown_work_404(client):
    assert client.get("/studio/works/w_nope/reading").status_code == 404
