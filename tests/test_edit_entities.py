# -*- coding: utf-8 -*-
"""素材 / 事实 / 术语可编辑 + 导入旧稿不算今天写字。

这三样原来只有「加」和「删」：抄错一个字就得整条重导，已抽事实卡的出处会一起断，
所以补了三条 PATCH。素材正文改了必须重建索引，否则 S_src 还在按旧文字检索。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import StudioStore

NORTH = "青云宗在山的北面，冬天下雪就封山。" * 20
SOUTH = "赤水城在山的南面，四季都通商。" * 20


class FakeGateway:
    """这些接口不调模型，这里只满足 app.state 的形状。"""

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


def make_work(client: TestClient, *, title: str) -> dict:
    return client.post("/studio/works", json={
        "kind": "article", "scale": "single", "title": title,
    }).json()


def add_source(client: TestClient, work_id: str, *, title: str = "地理", content: str = NORTH) -> dict:
    resp = client.post(f"/studio/works/{work_id}/sources", json={"title": title, "content": content})
    assert resp.status_code == 200, resp.text
    return resp.json()


def search(client: TestClient, q: str) -> dict:
    return client.get("/studio/search", params={"q": q, "limit": "20"}).json()


def test_source_content_change_rebuilds_the_index(tmp_path) -> None:
    """改正文却不重建索引，模型会继续引用已经删掉的那段资料。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="资料集")
        src = add_source(client, work["id"])
        assert search(client, "青云宗")["count"] == 1

        resp = client.patch(f"/studio/sources/{src['id']}", json={
            "title": "改了标题", "content": SOUTH,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "改了标题"

        assert search(client, "青云宗")["count"] == 0, "旧正文不该还搜得到"
        hit = search(client, "赤水城")["hits"][0]
        assert hit["source_kind"] == "source"
        assert hit["work_id"] == work["id"]


def test_source_title_only_edit_keeps_the_index(tmp_path) -> None:
    """只改标题时别把正文索引一起丢了：index_source 按 content_hash 自己跳过。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="资料集")
        src = add_source(client, work["id"])

        resp = client.patch(f"/studio/sources/{src['id']}", json={"title": "新标题"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "新标题"
        assert search(client, "青云宗")["count"] == 1


def test_source_patch_rejects_bad_input(tmp_path) -> None:
    with make_client(tmp_path) as client:
        work = make_work(client, title="资料集")
        src = add_source(client, work["id"])

        empty = client.patch(f"/studio/sources/{src['id']}", json={})
        assert empty.status_code == 400, empty.text
        blank = client.patch(f"/studio/sources/{src['id']}", json={"content": "   "})
        assert blank.status_code == 400, blank.text
        missing = client.patch("/studio/sources/s_nope", json={"title": "x"})
        assert missing.status_code == 404, missing.text

        # 拒掉的请求不能改坏原行
        still = client.get(f"/studio/sources/{src['id']}").json()
        assert still["content"].startswith("青云宗")
        assert still["title"] == "地理"


def test_fact_edit_keeps_the_source_link(tmp_path) -> None:
    """改事实卡的文字，不能把它跟素材的关系改丢——出处是 fact_stale 的依据。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="资料集")
        src = add_source(client, work["id"])
        fact = client.post(f"/studio/works/{work['id']}/facts", json={
            "statement": "青云宗在北面", "source_id": src["id"],
        }).json()

        resp = client.patch(f"/studio/facts/{fact['id']}", json={
            "statement": "青云宗在北面，冬天封山", "valid_until": "2026-12",
        })
        assert resp.status_code == 200, resp.text
        row = resp.json()
        assert row["statement"] == "青云宗在北面，冬天封山"
        assert row["valid_until"] == "2026-12"
        assert row["source_id"] == src["id"]
        assert row["source_title"] == "地理"

        listed = client.get(f"/studio/works/{work['id']}/facts").json()
        assert [f["statement"] for f in listed] == ["青云宗在北面，冬天封山"]


def test_fact_and_term_patch_validate_input(tmp_path) -> None:
    with make_client(tmp_path) as client:
        work = make_work(client, title="资料集")
        fact = client.post(f"/studio/works/{work['id']}/facts", json={"statement": "一"}).json()
        term = client.post(f"/studio/works/{work['id']}/terms", json={"term": "云原生"}).json()

        assert client.patch(f"/studio/facts/{fact['id']}", json={}).status_code == 400
        assert client.patch(f"/studio/facts/{fact['id']}", json={"statement": "  "}).status_code == 400
        assert client.patch("/studio/facts/f_nope", json={"statement": "x"}).status_code == 404
        assert client.patch(f"/studio/terms/{term['id']}", json={}).status_code == 400
        assert client.patch(f"/studio/terms/{term['id']}", json={"term": ""}).status_code == 400
        assert client.patch("/studio/terms/t_nope", json={"term": "x"}).status_code == 404


def test_term_edit_saves_preferred_and_forbidden(tmp_path) -> None:
    """禁用说法在库里是 JSON 数组，前端按中文逗号编辑，来回都得对得上。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="术语集")
        client.post(f"/studio/works/{work['id']}/terms", json={"term": "云原生"})
        term = client.get(f"/studio/works/{work['id']}/terms").json()[0]

        resp = client.patch(f"/studio/terms/{term['id']}", json={
            "preferred": "cloud native", "forbidden": ["云原生技术", "云原声"],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["preferred"] == "cloud native"
        assert json.loads(resp.json()["forbidden"]) == ["云原生技术", "云原声"]
        assert resp.json()["term"] == "云原生", "没传的字段不该被清空"


def test_term_rename_conflict_is_a_400_not_a_500(tmp_path) -> None:
    """改成已有术语是「重名」，不是系统故障：得给一句人话。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="术语集")
        client.post(f"/studio/works/{work['id']}/terms", json={"term": "甲"})
        client.post(f"/studio/works/{work['id']}/terms", json={"term": "乙"})
        terms = {t["term"]: t for t in client.get(f"/studio/works/{work['id']}/terms").json()}

        resp = client.patch(f"/studio/terms/{terms['乙']['id']}", json={"term": "甲"})
        assert resp.status_code == 400, resp.text
        assert "同名" in resp.json()["detail"]
        # 撞了之后不能留在半改状态
        assert client.get(f"/studio/works/{work['id']}/terms").json()[0]["id"]


def test_imported_nodes_hold_words_but_do_not_fake_today_s_writing(tmp_path) -> None:
    """导入旧稿是一次搬运，不是今天写了三千字：日历不能被旧稿刷高。"""
    with make_client(tmp_path) as client:
        work = make_work(client, title="旧稿")
        for i in (1, 2, 3):
            resp = client.post(f"/studio/works/{work['id']}/nodes", json={
                "kind": "section", "title": f"第 {i} 节", "content": NORTH,
            })
            assert resp.status_code == 200, resp.text
            assert resp.json()["word_count"] > 0

        tree = client.get(f"/studio/works/{work['id']}/tree").json()
        assert [n["ordinal"] for n in tree] == [1, 2, 3]
        assert all(n["word_count"] > 0 for n in tree)

        daily = client.get("/studio/stats/daily").json()
        assert daily["today"] == 0, daily
        assert daily["total"] == 0, daily
