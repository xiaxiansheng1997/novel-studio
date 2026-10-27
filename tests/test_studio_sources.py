"""素材链路：按本节检索、用量计数、单条详情、从素材抽事实卡。

原来的 S_src 是 `list_sources(limit=5)` 各取开头 1500 字，而面板上写着「按相关度取」——
第 6 条素材永远进不了 prompt，长素材里最相关的那段也从来读不到；`sources.used`
从建表起就在，却没有任何代码写它；素材到事实卡只能靠人手工抄。
这一组用例把这三件事都钉住：装配层直接调 `assemble`，接口层全程 HTTP。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import assembler, indexer, prompting
from app.studio.store import StudioStore

OPEN_RATE = "2026 年公众号打开率降到百分之一点三，标题党反而更差。" * 8
TYPO = "正文段落行距一点七，页边距留白，图片宽度不要超过屏幕。" * 8


class FakeGateway:
    """记下每次真正发出去的 system/prompt，全程不打网络。"""

    def __init__(self, reply: str = "模型写好的段落。") -> None:
        self.reply = reply
        self.streamed: list[dict] = []
        self.completed: list[dict] = []

    def resolve(self, model: str):
        return ("fake", "")

    async def stream(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        self.streamed.append({"model": model, "system": system, "prompt": prompt})
        yield SimpleNamespace(text=self.reply)

    async def complete(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        self.completed.append({"model": model, "system": system, "prompt": prompt})
        return SimpleNamespace(text=self.reply)


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


def make_client(tmp_path, gw: FakeGateway) -> TestClient:
    store = StudioStore(tmp_path / "studio.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.studio_store.init()
        yield
        await app.state.studio_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.state.gateway = gw
    app.state.registry = SimpleNamespace(route=lambda name: (name, {}), adapters={})
    app.include_router(studio_routes.router)
    return TestClient(app)


def make_article(client: TestClient, *, title: str = "怎么起标题") -> tuple[dict, dict]:
    work = client.post("/studio/works", json={
        "kind": "article", "scale": "single", "title": "素材测试",
        "premise": "讲公众号写作",
    }).json()
    node = client.post(f"/studio/works/{work['id']}/nodes", json={
        "kind": "section", "title": title,
    }).json()
    return work, node


# ==================== 装配层：按本节检索素材 ====================


async def test_sources_are_picked_by_relevance_not_recency(store) -> None:
    """两条素材各讲一件事：本节讲「打开率」，就不能把排版那条塞进 prompt。"""
    work = await store.create_work(kind="article", scale="single", title="检索素材",
                                   premise="讲公众号写作")
    node = await store.create_node(work_id=work["id"], kind="section",
                                   title="打开率为什么下降", ordinal=1)
    typo = await store.add_source(work["id"], content=TYPO, title="排版规范")
    rate = await store.add_source(work["id"], content=OPEN_RATE, title="打开率数据")
    # 两条都建索引：不建的话只能走「最近导入」的退路
    for sid in (typo["id"], rate["id"]):
        full = await store.get_source(sid)
        await indexer.index_source(store, full)

    res = await assembler.assemble(store, work, node, "article_draft", "写清楚打开率为什么掉")

    assert "打开率数据" in res.user
    assert "排版规范" not in res.user, "没命中的素材不该占预算"
    assert [s["id"] for s in res.used_sources] == [rate["id"]]
    assert not any("未建索引" in w for w in res.warnings), res.warnings


async def test_source_block_says_where_the_excerpt_came_from(store) -> None:
    """命中块要标「第几段、原文第几字起、全文多少字」，否则没法判断是不是断章取义。"""
    work = await store.create_work(kind="article", scale="single", title="出处", premise="公众号")
    node = await store.create_node(work_id=work["id"], kind="section", title="打开率", ordinal=1)
    created = await store.add_source(work["id"], content=OPEN_RATE, title="打开率数据")
    await indexer.index_source(store, await store.get_source(created["id"]))

    res = await assembler.assemble(store, work, node, "article_draft", "打开率")

    assert "《打开率数据》第 1 段" in res.user
    assert f"全文 {len(OPEN_RATE)} 字" in res.user


async def test_unindexed_sources_fall_back_but_say_so(store) -> None:
    """没建索引时仍然给「最近导入的开头」，但必须在 warnings 里说清这是退化的。"""
    work = await store.create_work(kind="article", scale="single", title="退路", premise="公众号")
    node = await store.create_node(work_id=work["id"], kind="section", title="打开率", ordinal=1)
    await store.add_source(work["id"], content=OPEN_RATE, title="打开率数据")

    res = await assembler.assemble(store, work, node, "article_draft", "打开率")

    assert "打开率数据" in res.user
    assert any("未建索引或本节没命中" in w for w in res.warnings), res.warnings
    assert len(res.used_sources) == 1


async def test_sixth_source_is_reachable_once_indexed(store) -> None:
    """旧写法固定取最新 5 条：第 6 条永远进不了 prompt。建了索引就该能被搜到。"""
    work = await store.create_work(kind="article", scale="single", title="第六条", premise="公众号")
    node = await store.create_node(work_id=work["id"], kind="section", title="古法排版", ordinal=1)
    for i in range(6):
        got = await store.add_source(work["id"], content=f"无关内容第 {i} 条。" * 40,
                                     title=f"噪声 {i}")
        await indexer.index_source(store, await store.get_source(got["id"]))
    await asyncio.sleep(0.01)
    target = await store.add_source(work["id"], content="古法排版讲究天头地脚，行距宽松。" * 10,
                                    title="古法排版讲义")
    await indexer.index_source(store, await store.get_source(target["id"]))

    res = await assembler.assemble(store, work, node, "article_draft", "古法排版")

    assert "古法排版讲义" in res.user


# ==================== 接口层：详情、导入即索引、用量 ====================


def test_source_full_text_is_readable_and_404_is_honest(tmp_path) -> None:
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work, _ = make_article(client)
        created = client.post(f"/studio/works/{work['id']}/sources",
                              json={"title": "打开率数据", "content": OPEN_RATE}).json()

        got = client.get(f"/studio/sources/{created['id']}")
        assert got.status_code == 200
        assert got.json()["content"] == OPEN_RATE
        assert client.get("/studio/sources/s_not_exist").status_code == 404


def test_import_indexes_immediately(tmp_path) -> None:
    """导入即建索引：否则第一次生成时，刚导的资料根本不会被按内容选中。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work, _ = make_article(client)
        client.post(f"/studio/works/{work['id']}/sources",
                    json={"title": "打开率数据", "content": OPEN_RATE})

        got = client.post(f"/studio/works/{work['id']}/search",
                          json={"q": "打开率", "scope": "source", "top_k": 5})
        assert got.status_code == 200, got.text
        assert got.json()["count"] >= 1
        assert "打开率数据" in [h["title"] for h in got.json()["hits"]]


def test_used_counter_only_moves_when_prompt_is_sent(tmp_path) -> None:
    """预览只算不调模型，不该把 used 加上去；真生成一次才 +1。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work, node = make_article(client)
        client.post(f"/studio/works/{work['id']}/sources",
                    json={"title": "打开率数据", "content": OPEN_RATE})

        preview = client.post(
            f"/studio/context/preview?work_id={work['id']}&node_id={node['id']}",
            json={"task_type": "article_draft", "instruction": ""},
        )
        assert preview.status_code == 200
        assert preview.json()["used_sources"][0]["title"] == "打开率数据"
        assert client.get(f"/studio/works/{work['id']}/sources").json()[0]["used"] == 0

        resp = client.post(f"/studio/nodes/{node['id']}/generate",
                           json={"task_type": "article_draft", "instruction": ""})
        assert resp.status_code == 200, resp.text
        assert client.get(f"/studio/works/{work['id']}/sources").json()[0]["used"] == 1
        # 发出去的 prompt 里真的有素材，否则这次的计数就是假的
        assert "打开率数据" in gw.streamed[0]["prompt"]


def test_assembly_warnings_reach_the_meta_event(tmp_path) -> None:
    """装配阶段的告警必须随 meta 事件送到前端：只写进日志等于作者永远看不到。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        _, node = make_article(client)
        resp = client.post(f"/studio/nodes/{node['id']}/generate",
                           json={"task_type": "article_draft", "instruction": ""})
        assert resp.status_code == 200
        assert any("生成质量会明显下降" in w for w in _meta_events(resp.text))


def _meta_events(body: str) -> list[str]:
    """从 SSE 文本里取 meta 事件的 warnings（TestClient 会把整条流读完再返回）。"""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "meta":
            return ev.get("warnings") or []
    return []


# ==================== 素材 → 事实卡 ====================


def test_extract_facts_from_a_source(tmp_path) -> None:
    reply = json.dumps([
        {"statement": "2026 年公众号打开率降到 1.3%", "valid_until": "2026-12"},
        {"statement": "标题党反而更差", "valid_until": ""},
    ], ensure_ascii=False)
    gw = FakeGateway(reply=reply)
    with make_client(tmp_path, gw) as client:
        work, _ = make_article(client)
        created = client.post(f"/studio/works/{work['id']}/sources",
                              json={"title": "打开率数据", "content": OPEN_RATE}).json()

        got = client.post(f"/studio/sources/{created['id']}/facts/extract", json={"limit": 2})
        assert got.status_code == 200, got.text
        assert got.json()["added"] == 2

        facts = client.get(f"/studio/works/{work['id']}/facts").json()
        # 列表按 created_at DESC，两条同批写入的先后不保证，比内容就好
        assert sorted(f["statement"] for f in facts) == sorted([
            "2026 年公众号打开率降到 1.3%", "标题党反而更差",
        ])
        assert all(f["source_title"] == "打开率数据" for f in facts), facts
        # 抽事实卡不该顺手改正文，也不该记一次「素材被用了」
        assert client.get(f"/studio/works/{work['id']}/sources").json()[0]["used"] == 0


def test_extract_facts_rejects_unusable_output(tmp_path) -> None:
    gw = FakeGateway(reply="抱歉，我无法完成这个请求。")
    with make_client(tmp_path, gw) as client:
        work, _ = make_article(client)
        created = client.post(f"/studio/works/{work['id']}/sources",
                              json={"title": "打开率数据", "content": OPEN_RATE}).json()
        got = client.post(f"/studio/sources/{created['id']}/facts/extract", json={})
        assert got.status_code == 400
        assert client.get(f"/studio/works/{work['id']}/facts").json() == []


def test_parse_facts_drops_short_and_duplicate_lines() -> None:
    got = prompting.parse_facts(json.dumps([
        {"statement": "太短"},
        {"statement": "2026 年打开率 1.3%"},
        {"statement": "2026 年打开率 1.3%"},
        {"fact": "页边距留白 24 像素"},
    ], ensure_ascii=False))
    assert [f["statement"] for f in got] == ["2026 年打开率 1.3%", "页边距留白 24 像素"]
