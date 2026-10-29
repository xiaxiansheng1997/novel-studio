# -*- coding: utf-8 -*-
"""选题池 + 按天净增字数。

字数口径在 `node_versions` 上现算：版本表存全文快照，直接 SUM 长度会把同一段字
按保存次数重复计，所以按「这一版减上一版」的增量算，删改（负增量）不计。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import StudioStore


class FakeGateway:
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


def make_article(client: TestClient) -> tuple[dict, dict]:
    work = client.post("/studio/works", json={
        "kind": "article", "scale": "single", "title": "统计", "premise": "按天算字数",
    }).json()
    node = client.post(f"/studio/works/{work['id']}/nodes", json={
        "kind": "section", "title": "第一节", "content": "",
    }).json()
    return work, node


# ==================== 选题池 ====================

def test_idea_can_be_captured_and_then_used(tmp_path) -> None:
    """一句话想法：记下来 → 用它开写（记下 work_id）→ 不再出现在待写列表里。"""
    with make_client(tmp_path) as client:
        idea = client.post("/studio/ideas", json={"text": "公众号排版避坑：写给第一次排版的人"}).json()
        assert idea["status"] == "open" and idea["work_id"] == ""
        assert [i["id"] for i in client.get("/studio/ideas").json()] == [idea["id"]]

        work, _ = make_article(client)
        used = client.patch(f"/studio/ideas/{idea['id']}",
                            json={"status": "used", "work_id": work["id"]})
        assert used.status_code == 200, used.text
        assert used.json()["work_id"] == work["id"]

        assert client.get("/studio/ideas").json() == []
        everything = client.get("/studio/ideas", params={"status": ""}).json()
        assert [i["status"] for i in everything] == ["used"]


def test_idea_rejects_blank_and_unknown_status(tmp_path) -> None:
    with make_client(tmp_path) as client:
        assert client.post("/studio/ideas", json={"text": ""}).status_code == 422
        assert client.post("/studio/ideas", json={"text": "   "}).status_code == 400
        assert client.post("/studio/ideas", json={"text": "x" * 501}).status_code == 422

        idea = client.post("/studio/ideas", json={"text": "写点什么"}).json()
        assert client.patch(f"/studio/ideas/{idea['id']}",
                            json={"status": "done"}).status_code == 422
        assert client.delete(f"/studio/ideas/{idea['id']}").status_code == 200
        assert client.delete(f"/studio/ideas/{idea['id']}").status_code == 404
        assert client.patch(f"/studio/ideas/{idea['id']}", json={"text": "改了"}).status_code == 404


def test_idea_text_can_be_reworded(tmp_path) -> None:
    with make_client(tmp_path) as client:
        idea = client.post("/studio/ideas", json={"text": "旧的说法"}).json()
        got = client.patch(f"/studio/ideas/{idea['id']}", json={"text": "新的说法"}).json()
        assert got["text"] == "新的说法"


# ==================== 按天字数 ====================

def test_daily_writes_counts_growth_not_snapshots(tmp_path) -> None:
    """保存三次（100 → 160 → 120）只算 160 字：快照长度直接相加会变成 380。"""
    with make_client(tmp_path) as client:
        _, node = make_article(client)
        for n in (100, 160, 120):
            resp = client.patch(f"/studio/nodes/{node['id']}",
                                json={"content": "一" * n, "snapshot": True})
            assert resp.status_code == 200, resp.text

        stats = client.get("/studio/stats/daily", params={"days": 7}).json()
        assert stats["today"] == 160, stats
        assert stats["total"] == 160
        assert stats["saves_today"] == 3
        assert stats["streak"] == 1
        assert len(stats["days"]) == 1
        assert stats["days"][0]["saves"] == 3


def test_manual_auto_save_counts_even_without_a_version(tmp_path) -> None:
    """纯手打不留版本，统计也必须算上——这正是不能在版本表上算的原因。"""
    with make_client(tmp_path) as client:
        _, node = make_article(client)
        client.patch(f"/studio/nodes/{node['id']}", json={"content": "二" * 300})
        stats = client.get("/studio/stats/daily").json()
        assert stats["today"] == 300
        assert stats["saves_today"] == 1


def test_ai_adoption_does_not_count_as_writing(tmp_path) -> None:
    """AI 采用的正文不算「我写的」：否则夜里跑一遍整篇，早上的统计会显得像自己写了三万字。"""
    with make_client(tmp_path) as client:
        _, node = make_article(client)
        client.patch(f"/studio/nodes/{node['id']}", json={
            "content": "三" * 500, "snapshot": True, "snapshot_source": "ai_draft",
        })
        stats = client.get("/studio/stats/daily").json()
        assert stats["today"] == 0
        assert stats["saves_today"] == 0


def test_restoring_an_old_version_does_not_count(tmp_path) -> None:
    """回滚是换回旧稿，不是新写的字。"""
    with make_client(tmp_path) as client:
        _, node = make_article(client)
        client.patch(f"/studio/nodes/{node['id']}", json={"content": "四" * 300, "snapshot": True})
        client.patch(f"/studio/nodes/{node['id']}", json={"content": "四" * 100, "snapshot": True})
        oldest = client.get(f"/studio/nodes/{node['id']}/versions").json()[-1]["id"]
        # 换回 300 字那一版：正增量 +200，但那是回滚不是写作
        client.post(f"/studio/nodes/{node['id']}/versions/{oldest}/restore")
        assert client.get("/studio/stats/daily").json()["today"] == 300


def test_streak_keeps_counting_when_today_is_empty() -> None:
    """今天还没写不算断：从昨天往回数，否则中午打开会看到连续天数突然归零。"""
    today = date(2026, 10, 29)
    rows = [
        {"day": (today - timedelta(days=i)).isoformat(), "chars": 100, "saves": 1}
        for i in (1, 2, 3)
    ]
    assert studio_routes.streak_days(rows, today) == 3
    assert studio_routes.streak_days([], today) == 0
    assert studio_routes.streak_days(rows + [{"day": today.isoformat(), "chars": 1, "saves": 1}],
                                     today) == 4
    # 中间断一天就只数到断点
    gap = rows + [{"day": (today - timedelta(days=5)).isoformat(), "chars": 9, "saves": 1}]
    assert studio_routes.streak_days(gap, today) == 3
