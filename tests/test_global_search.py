# -*- coding: utf-8 -*-
"""书架检索 / 归档 + 跨作品全文搜索。

原先 `POST /studio/works/{id}/search` 只能在某一篇里搜，书架也只有一个平铺列表：
作品一多，「我之前在哪篇写过这个」和「那篇写完了的先收起来」都没有入口。
这里的用例只走 HTTP：aiosqlite 的连接绑在建立它的事件循环上，不能让 store 在 pytest 循环里初始化。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import StudioStore

LAYOUT = "公众号排版先看行距与留白，再看字号，最后才是颜色。" * 120
TITLE = "列表页只露前二十个字，标题超过就会被截断。" * 40


class FakeGateway:
    """搜索不调模型，这里只满足 app.state 的形状。"""

    def resolve(self, model: str):
        return ("fake", "")

    async def stream(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        yield SimpleNamespace(text="")

    async def complete(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        return SimpleNamespace(text="")


def make_client(tmp_path) -> TestClient:
    store = StudioStore(tmp_path / "studio.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.state.gateway = FakeGateway()
    app.state.registry = SimpleNamespace(route=lambda name: (name, {}), adapters={})
    app.include_router(studio_routes.router)
    return TestClient(app)


def make_work(client: TestClient, *, title: str, premise: str = "", sections=()) -> dict:
    work = client.post("/studio/works", json={
        "kind": "article", "scale": "single", "title": title, "premise": premise,
    }).json()
    for i, (name, body) in enumerate(sections, start=1):
        client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "section", "title": name, "content": body,
        })
    got = client.get(f"/studio/works/{work['id']}/tree").json()
    assert len([n for n in got if n["ordinal"] is not None]) == len(sections)
    return work


def index(client: TestClient, work_id: str) -> None:
    resp = client.post(f"/studio/works/{work_id}/index", json={"force": False, "include_sources": True})
    assert resp.status_code == 200, resp.text


def test_search_crosses_works_and_names_the_section(tmp_path) -> None:
    """跨作品搜：结果要说清「哪一篇的哪一节」，否则点进去还得自己再找一遍。"""
    with make_client(tmp_path) as client:
        a = make_work(client, title="排版手册", sections=[("第一讲", LAYOUT)])
        b = make_work(client, title="标题实验", sections=[("标题长度", TITLE)])
        index(client, a["id"])
        index(client, b["id"])

        body = client.get("/studio/search", params={"q": "排版"}).json()
        assert body["count"] == 1, body
        hit = body["hits"][0]
        assert hit["work_id"] == a["id"]
        assert hit["work_title"] == "排版手册"
        assert hit["node_title"] == "第一讲"
        assert hit["node_id"] and hit["ordinal"] == 1
        assert "排版" in hit["snippet"]
        assert len(hit["snippet"]) <= 150, "片段要截短，不能把整块正文塞进列表"

        other = client.get("/studio/search", params={"q": "截断"}).json()
        assert [h["work_title"] for h in other["hits"]] == ["标题实验"]


def test_search_finds_a_work_by_title_without_any_index(tmp_path) -> None:
    """正文 FTS 不索引标题：只靠它搜书名会零命中，而人找东西常常只记得名字。"""
    with make_client(tmp_path) as client:
        make_work(client, title="公众号排版避坑", premise="写给第一次排版的人")
        body = client.get("/studio/search", params={"q": "排版避坑"}).json()
        assert body["hits"] == []
        assert [w["title"] for w in body["works"]] == ["公众号排版避坑"]

        by_premise = client.get("/studio/search", params={"q": "第一次排版"}).json()
        assert [w["title"] for w in by_premise["works"]] == ["公众号排版避坑"]


def test_one_hit_per_section(tmp_path) -> None:
    """同一节被切成很多块时只留一块：这张表答的是「去哪几节看」。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="长文", sections=[
            ("第一节", LAYOUT), ("第二节", "排版之外还有标点：" + LAYOUT), ("第三节", "标题另说"),
        ])
        index(client, work["id"])
        body = client.get("/studio/search", params={"q": "排版", "limit": "50"}).json()
        node_ids = [h["node_id"] for h in body["hits"]]
        assert len(node_ids) == len(set(node_ids)) == 2, node_ids


def test_empty_query_is_not_an_error(tmp_path) -> None:
    with make_client(tmp_path) as client:
        for params in ({}, {"q": ""}, {"q": "   "}):
            resp = client.get("/studio/search", params=params)
            assert resp.status_code == 200, resp.text
            assert resp.json()["hits"] == []


def test_archive_hides_from_shelf_but_keeps_the_work(tmp_path) -> None:
    """归档只是从书架收起：正文、版本、索引都不动，随时能恢复。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="写完了先收起来", sections=[("一", LAYOUT)])

        bad = client.patch(f"/studio/works/{work['id']}", json={"status": "deleted"})
        assert bad.status_code == 422, bad.text

        ok = client.patch(f"/studio/works/{work['id']}", json={"status": "archived"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "archived"

        active = client.get("/studio/works", params={"status": "active"}).json()
        assert work["id"] not in {w["id"] for w in active}
        archived = client.get("/studio/works", params={"status": "archived"}).json()
        assert [w["id"] for w in archived] == [work["id"]]
        # 归档之后照样打得开、搜得到
        assert client.get(f"/studio/works/{work['id']}").status_code == 200
        index(client, work["id"])
        assert client.get("/studio/search", params={"q": "排版"}).json()["count"] == 1

        back = client.patch(f"/studio/works/{work['id']}", json={"status": "active"})
        assert back.json()["status"] == "active"
        assert work["id"] in {w["id"] for w in client.get("/studio/works", params={"status": "active"}).json()}


def test_archived_work_still_appears_in_search_results(tmp_path) -> None:
    """归档不是删除：旧文里的一句话仍然要搜得到，否则等于自己把资料埋了。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="旧文", sections=[("旧节", LAYOUT)])
        index(client, work["id"])
        client.patch(f"/studio/works/{work['id']}", json={"status": "archived"})

        body = client.get("/studio/search", params={"q": "排版"}).json()
        assert body["count"] == 1
        assert body["hits"][0]["work_status"] == "archived"


def test_search_snippet_is_json_safe(tmp_path) -> None:
    """片段里可能有引号/换行：响应必须是合法 JSON，前端才解析得动。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="符号", sections=[
            ("一节", '他说：“排版"要用全角引号’\n\n第二段还有换行。"' * 20),
        ])
        index(client, work["id"])
        resp = client.get("/studio/search", params={"q": "全角引号"})
        assert resp.status_code == 200
        assert json.loads(resp.text)["count"] == 1
