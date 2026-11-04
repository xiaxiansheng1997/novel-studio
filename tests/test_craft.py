"""手艺拆解：三级分析的落库/增量/依赖，与四个路由端点的入队、拷贝、抽模板。

测试口径：store 用 tmp_path 里的真 StudioStore（迁移与唯一约束都要真跑），
模型全部走 FakeGateway 记账——拆解一次烧多少调用，看 calls 清单就知道。
路由层用 ASGITransport 与测试同循环：store 可以直接 await（造卡不用绕 HTTP）。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import studio_routes
from app.llm.gateway import DEFAULT_MODEL
from app.studio import craft, tasks
from app.studio.store import StudioStore, _hash

CHAP_TEXT = "她推门进去，屋里坐着三个人，气氛一下子僵住了。" * 20  # ~500 字，够拆
SHORT_TEXT = "他醒了。" * 20  # 80 字，低于 MIN_CHAPTER_CHARS，不该拆

CHAPTER_DATA = {"scenes": [{"unit": "对峙开场", "conflict": "主角与房东争租约",
                            "info_gap": "读者已知合同是假的"}],
                "hook": "新威胁：律师函出现", "role": "升级"}
VOLUME_DATA = {"arc": "从忍让到反击", "escalation": "口头争执→诉讼",
               "turning_points": [{"at": "第2章", "what": "主角决定应诉"}],
               "foreshadows": [{"plant": "第1章", "payoff": "未收", "what": "假合同来源"}]}
BOOK_DATA = {"structure_type": "三幕式",
             "acts": [{"name": "铺垫", "span": "1-3章", "function": "立起对抗"},
                      {"name": "升级", "span": "4-8章", "function": "冲突可见化"},
                      {"name": "收束", "span": "9-10章", "function": "反转后落定"}],
             "main_lines": ["租约之战"], "rhythm": "高压段两章舒缓一章",
             "takeaways": ["开局第三章就让主角付出代价"]}


def reply_for(system: str) -> str:
    if system == craft.CHAPTER_SYSTEM:
        return json.dumps(CHAPTER_DATA, ensure_ascii=False)
    if system == craft.VOLUME_SYSTEM:
        return json.dumps(VOLUME_DATA, ensure_ascii=False)
    if system == craft.BOOK_SYSTEM:
        return json.dumps(BOOK_DATA, ensure_ascii=False)
    return "{}"


class FakeGateway:
    """只记账 + 按 system 认层级返 JSON。resolve 给 provider 名。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        self.calls.append((system, prompt))
        return SimpleNamespace(text=reply_for(system))

    def resolve(self, model: str):
        return ("fake", model)


class FakeRegistry:
    """可选 priced：compat 单价通道，用来测 402 闸门。"""

    def __init__(self, priced: bool = False) -> None:
        self.adapters: dict = {}
        self.priced = priced

    def route(self, model: str):
        cfg = SimpleNamespace(compat=self.priced, models=[], key="fake",
                              price_in_per_1k_tokens=2.0 if self.priced else 0.0,
                              price_out_per_1k_tokens=8.0 if self.priced else 0.0)
        return SimpleNamespace(cfg=cfg, key="fake")


class FakeQueue:
    async def start(self) -> None:
        pass

    async def status(self, work_id: str = "") -> dict:
        return {"worker": False, "counts": {}}


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def make_book(store, *, chapters: int = 2, with_volume: bool = True):
    """一本真的「被拆的书」：1 卷 N 章，章章有正文（长一章短一章便于过滤测试）。"""
    work = await store.create_work(kind="novel", title="拆解测试书", premise="都市维权")
    vol = None
    if with_volume:
        vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    nodes = []
    for i in range(chapters):
        text = CHAP_TEXT if i % 3 else SHORT_TEXT
        n = await store.create_node(work_id=work["id"], kind="chapter",
                                    title=f"第{i + 1}章", content=text,
                                    parent_id=vol["id"] if vol else None)
        nodes.append(await store.get_node(n["id"]))
    return work, vol, nodes


# ==================== 执行层：章 ====================

async def test_chapter_card_persisted_and_rendered(store) -> None:
    gw = FakeGateway()
    work, _vol, nodes = await make_book(store)
    node = next(n for n in nodes if len(n["content"]) >= craft.MIN_CHAPTER_CHARS)

    res = await craft.analyze_chapter(gw, store, node, model=DEFAULT_MODEL)

    assert res["status"] == "ok"
    row = await store.get_craft(work["id"], "chapter", node["id"])
    assert row and row["ref_hash"] == _hash(node["content"])
    assert "冲突" in row["text"] and row["note"] == ""
    data = json.loads(row["content_json"])
    assert data["role"] == "升级"
    assert len(gw.calls) == 1


async def test_chapter_skips_when_hash_unchanged(store) -> None:
    gw = FakeGateway()
    _work, _vol, nodes = await make_book(store)
    node = next(n for n in nodes if len(n["content"]) >= craft.MIN_CHAPTER_CHARS)

    await craft.analyze_chapter(gw, store, node, model=DEFAULT_MODEL)
    again = await craft.analyze_chapter(gw, store, node, model=DEFAULT_MODEL)
    assert again["status"] == "skipped" and len(gw.calls) == 1

    forced = await craft.analyze_chapter(gw, store, node, model=DEFAULT_MODEL, force=True)
    assert forced["status"] == "ok" and len(gw.calls) == 2


async def test_chapter_truncates_long_body(store) -> None:
    gw = FakeGateway()
    work = await store.create_work(kind="novel", title="长章书")
    node = await store.create_node(work_id=work["id"], kind="chapter", title="超长章")
    await store.update_node(node["id"], content="正文段落。" * 4000)  # 2 万字
    full = await store.get_node(node["id"])

    await craft.analyze_chapter(gw, store, full, model=DEFAULT_MODEL)

    row = await store.get_craft(work["id"], "chapter", node["id"])
    assert "截" in row["note"]
    _sys, prompt = gw.calls[0]
    assert len(prompt) < craft.CHAPTER_MAX_CHARS + 600  # 只带前段，不是整本两万字


async def test_short_chapter_not_analysed(store) -> None:
    gw = FakeGateway()
    _work, _vol, nodes = await make_book(store)
    node = next(n for n in nodes if len(n["content"]) < craft.MIN_CHAPTER_CHARS)

    res = await craft.analyze_chapter(gw, store, node, model=DEFAULT_MODEL)
    assert res["status"] == "short" and gw.calls == []


# ==================== 执行层：卷与全书 ====================

async def test_volume_waits_for_all_chapters(store) -> None:
    """真的缺卡（长章没拆）不放行；短章不算缺——那是两回事。"""
    gw = FakeGateway()
    work = await store.create_work(kind="novel", title="待齐书")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    a = await store.create_node(work_id=work["id"], kind="chapter", title="第1章",
                                content=CHAP_TEXT, parent_id=vol["id"])
    b = await store.create_node(work_id=work["id"], kind="chapter", title="第2章",
                                content=CHAP_TEXT, parent_id=vol["id"])
    await craft.analyze_chapter(gw, store, await store.get_node(a["id"]),
                                model=DEFAULT_MODEL)

    with pytest.raises(ValueError, match="未拆解"):
        await craft.analyze_volume(gw, store, work, vol, model=DEFAULT_MODEL)

    # 补齐第二张章卡后卷级才放行
    await craft.analyze_chapter(gw, store, await store.get_node(b["id"]),
                                model=DEFAULT_MODEL)
    res = await craft.analyze_volume(gw, store, work, vol, model=DEFAULT_MODEL)
    assert res["status"] == "ok"
    row = await store.get_craft(work["id"], "volume", vol["id"])
    assert "伏笔" in row["text"]


async def test_volume_ignores_short_chapters(store) -> None:
    """短章入队时就被过滤、永远不会产卡片：拿它算「未拆解」会让卷级永远等不到。"""
    gw = FakeGateway()
    work, vol, nodes = await make_book(store, chapters=2)  # 一章长一章短
    assert any(len(n["content"]) < craft.MIN_CHAPTER_CHARS for n in nodes)
    for n in nodes:
        if len(n["content"]) >= craft.MIN_CHAPTER_CHARS:
            await craft.analyze_chapter(gw, store, n, model=DEFAULT_MODEL)

    res = await craft.analyze_volume(gw, store, work, vol, model=DEFAULT_MODEL)
    assert res["status"] == "ok"
    row = await store.get_craft(work["id"], "volume", vol["id"])
    assert row and "弧线" in row["text"]


async def test_book_falls_back_to_chapter_cards(store) -> None:
    gw = FakeGateway()
    work = await store.create_work(kind="novel", title="单层书")
    node = await store.create_node(work_id=work["id"], kind="chapter",
                                   title="独章", content=CHAP_TEXT)
    full = await store.get_node(node["id"])
    await craft.analyze_chapter(gw, store, full, model=DEFAULT_MODEL)

    res = await craft.analyze_book(gw, store, work, model=DEFAULT_MODEL)
    assert res["status"] == "ok"
    row = await store.get_craft(work["id"], "book", "")
    assert "幕结构" in row["text"] and "开局第三章" in row["text"]


async def test_run_task_dispatches_craft(store) -> None:
    """队列入口：REGISTRY 里查得到 craft，payload.level 决定层级。"""
    assert "craft" in tasks.REGISTRY
    gw = FakeGateway()
    work, _vol, nodes = await make_book(store)
    node = next(n for n in nodes if len(n["content"]) >= craft.MIN_CHAPTER_CHARS)
    ctx = tasks.TaskContext(store=store, gateway=gw, work=work, node=node,
                            payload={"level": "chapter", "force": False},
                            model=DEFAULT_MODEL)
    res = await tasks.run_task(ctx, "craft")
    assert res["status"] == "ok"
    with pytest.raises(ValueError):
        await tasks.run_task(ctx, "craft_unknown")  # 未知 task_type 兜底还在


# ==================== 路由层 ====================

@asynccontextmanager
async def _app(tmp_path, *, priced: bool = False):
    """完整 HTTP 链：ASGITransport 与测试同循环，store 可直接 await 造数据。"""
    st = StudioStore(tmp_path / "studio.db")
    await st.init()
    app = FastAPI()
    app.state.settings = SimpleNamespace(api_key="", allow_origins="",
                                         host="127.0.0.1", port=8000)
    app.state.studio_store = st
    app.state.gateway = FakeGateway()
    app.state.registry = FakeRegistry(priced=priced)
    app.state.studio_queue = FakeQueue()
    app.include_router(studio_routes.router)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, st
    finally:
        await st.close()


@pytest.fixture
async def craft_app(tmp_path):
    async with _app(tmp_path) as pair:
        yield pair


@pytest.fixture
async def craft_app_priced(tmp_path):
    async with _app(tmp_path, priced=True) as pair:
        yield pair


async def _http_book(client) -> tuple[dict, dict, list[dict]]:
    """通过 HTTP 造一本 3 章 1 卷的书（两章够字、一章太短）。"""
    work = (await client.post("/studio/works", json={
        "kind": "novel", "title": "拆解书", "premise": "测试", "scale": "single"})).json()
    vol = (await client.post(f"/studio/works/{work['id']}/nodes",
                             json={"kind": "volume", "title": "第一卷"})).json()
    nodes = []
    for i, text in enumerate([CHAP_TEXT, CHAP_TEXT, SHORT_TEXT]):
        n = (await client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "chapter", "title": f"第{i + 1}章",
            "parent_id": vol["id"], "content": text})).json()
        nodes.append(n)
    return work, vol, nodes


async def _http_two_volumes(client) -> tuple[dict, list[dict], list[dict]]:
    """两卷各两章：范围拆要验证「只铺本卷、不动旁卷」。"""
    work = (await client.post("/studio/works", json={
        "kind": "novel", "title": "两卷书", "premise": "测试", "scale": "single"})).json()
    vols: list[dict] = []
    nodes: list[dict] = []
    for v in range(2):
        vol = (await client.post(f"/studio/works/{work['id']}/nodes",
                                 json={"kind": "volume", "title": f"第{v + 1}卷"})).json()
        vols.append(vol)
        for i in range(2):
            n = (await client.post(f"/studio/works/{work['id']}/nodes", json={
                "kind": "chapter", "title": f"卷{v + 1}第{i + 1}章",
                "parent_id": vol["id"], "content": CHAP_TEXT})).json()
            nodes.append(n)
    return work, vols, nodes


async def test_enqueue_scope_limits_to_one_volume(craft_app) -> None:
    """范围拆：只铺本卷的章 + 本卷汇总，全书级不排。"""
    client, _st = craft_app
    work, vols, nodes = await _http_two_volumes(client)

    r = await client.post(f"/studio/works/{work['id']}/craft",
                          json={"scope_node_id": vols[0]["id"]})
    assert r.status_code == 200
    j = r.json()
    assert j["chapters"] == 2           # 只数本卷
    assert j["queued"] == 3            # 2 章 + 1 卷；全书级被拿掉
    assert "全书级等各卷拆完" in j["note"]

    jobs = [x for x in (await client.get(f"/studio/jobs?work_id={work['id']}")).json()
            if x["task_type"] == "craft"]
    assert sorted(x["priority"] for x in jobs) == [5, 6, 6]
    assert {x["node_id"] for x in jobs} == {
        vols[0]["id"], nodes[0]["id"], nodes[1]["id"]}

    # 范围只能到卷：传章 id 直接拒；只勾全书 + 范围也是空计划
    bad = await client.post(f"/studio/works/{work['id']}/craft",
                            json={"scope_node_id": nodes[0]["id"]})
    assert bad.status_code == 400 and "不能选到章" in bad.json()["detail"]
    only_book = await client.post(f"/studio/works/{work['id']}/craft",
                                  json={"scope_node_id": vols[0]["id"],
                                        "levels": ["book"]})
    assert only_book.status_code == 400


async def test_enqueue_orders_and_estimates(craft_app) -> None:
    client, _st = craft_app
    work, _vol, _nodes = await _http_book(client)

    r = await client.post(f"/studio/works/{work['id']}/craft", json={})
    assert r.status_code == 200
    j = r.json()
    assert j["chapters"] == 2                 # 短章被过滤：没东西可拆
    assert j["queued"] == 4 and j["deduped"] == 0   # 2 章 + 1 卷 + 1 书
    assert j["estimate"]["priced"] is False   # 反代不计钱，不拦

    jobs = [x for x in (await client.get(f"/studio/jobs?work_id={work['id']}")).json()
            if x["task_type"] == "craft"]
    # claim_job 是 priority DESC：章(6)先跑、书(4)最后，依赖顺序靠这张表保证
    assert sorted(x["priority"] for x in jobs) == [4, 5, 6, 6]

    r2 = await client.post(f"/studio/works/{work['id']}/craft", json={})
    assert r2.json()["queued"] == 0 and r2.json()["deduped"] == 4  # 幂等去重

    g = (await client.get(f"/studio/works/{work['id']}/craft")).json()
    assert g["total_chapters"] == 2 and g["analysed_chapters"] == 0
    assert g["progress"].get("queued") == 4
    assert g["analyses"] == []                # worker 没跑，还没有产物


async def test_enqueue_budget_gate(craft_app_priced) -> None:
    client, _st = craft_app_priced
    work, _vol, _nodes = await _http_book(client)
    await client.put("/studio/budget", json={"limit": 0.0001})

    r = await client.post(f"/studio/works/{work['id']}/craft", json={})
    assert r.status_code == 402               # 微预算 + 计钱通道：先拦再说
    r2 = await client.post(f"/studio/works/{work['id']}/craft",
                           json={"ignore_budget": True})
    assert r2.status_code == 200              # 拦的是手滑，不拦清醒的决定


async def test_apply_copies_cards_as_indexed_sources(craft_app) -> None:
    client, st = craft_app
    work_a, _v, nodes = await _http_book(client)
    work_b = (await client.post("/studio/works",
                                json={"kind": "novel", "title": "新书"})).json()
    c1 = await st.upsert_craft(work_a["id"], level="chapter", ref_node_id=nodes[0]["id"],
                               title="第1章", text="冲突：主角与房东争租约", ref_hash="h1")
    c2 = await st.upsert_craft(work_a["id"], level="book", ref_node_id="",
                               title=work_a["title"], text="结构类型：三幕式", ref_hash="h2")

    r = await client.post(f"/studio/works/{work_a['id']}/craft/apply", json={
        "target_work_id": work_b["id"], "analysis_ids": [c1["id"], c2["id"]]})
    assert r.status_code == 200 and r.json()["ok"] == 2

    sources = await st.list_sources(work_b["id"])
    assert len(sources) == 2
    assert all(s["title"].startswith("【结构】") for s in sources)
    # 立即建索引：S_src 桶检索的前提（没索引的素材只能拿开头 1500 字凑数）
    for s in sources:
        assert await st.index_stamp("source", s["id"])

    bad = await client.post(f"/studio/works/{work_a['id']}/craft/apply", json={
        "target_work_id": work_b["id"], "analysis_ids": ["cr_不存在"]})
    assert bad.status_code == 400


async def test_from_craft_creates_template(craft_app) -> None:
    client, st = craft_app
    work, _v, _nodes = await _http_book(client)

    other = (await client.post("/studio/works",
                               json={"kind": "novel", "title": "没拆过"})).json()
    r400 = await client.post(f"/studio/templates/from-craft/{other['id']}", json={})
    assert r400.status_code == 400            # 没拆过就抽模板：先跑全书级

    await st.upsert_craft(work["id"], level="book", ref_node_id="",
                          title=work["title"], text="幕结构",
                          content_json=json.dumps(BOOK_DATA, ensure_ascii=False),
                          ref_hash="h")
    r = await client.post(f"/studio/templates/from-craft/{work['id']}",
                          json={"name": "拆出来的骨架"})
    assert r.status_code == 200
    tpl = r.json()
    outline = json.loads(tpl["outline_json"]) if isinstance(tpl["outline_json"], str) \
        else tpl["outline_json"]
    assert [o["heading"] for o in outline] == ["铺垫", "升级", "收束"]
    assert all(o["target_words"] > 0 for o in outline)
    assert tpl["source_work_id"] == work["id"]
    assert "结构拆解" in (tpl["tags_json"] or "")
