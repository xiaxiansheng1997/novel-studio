"""双开与异地的乐观锁：手里那份比库里旧，盖之前必须先问一声。

只拦改正文的写入，而且只拦带 base 的调用方——没带 base 的（服务端内部写入、
旧调用）无从判断对方见过哪一份，维持 last-write-wins 的原行为。
409 把服务端整份带回（冲突条的「重取」按钮直接消费它，不用二次 GET）；
ignore_conflict 是人看过提示之后的放行。
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import studio_routes
from app.studio.store import StudioStore


@pytest.fixture
async def studio(tmp_path):
    """同一事件循环里起 store 与 ASGI 客户端：aiosqlite 的连接不能跨循环用。"""
    store = StudioStore(tmp_path / "studio.db")
    await store.init()
    app = FastAPI()
    app.state.settings = SimpleNamespace(api_key="", allow_origins="", host="127.0.0.1", port=8000)
    app.state.studio_store = store
    app.include_router(studio_routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store
    await store.close()


async def make_node(store) -> dict:
    a = await store.create_work(kind="novel", title="冲突甲")
    return await store.create_node(work_id=a["id"], kind="chapter", title="第一节")


async def test_stale_base_blocked_then_force(studio) -> None:
    """取到存之间被别处写了：旧 base 的 PATCH 吃 409，看过之后可强存。"""
    client, store = studio
    n = await make_node(store)
    base = n["updated_at"]

    # 另一个窗口/异地/跑批先落了一笔（它带的也是这个 base，先到先得）
    r1 = await client.patch(f"/studio/nodes/{n['id']}", json={
        "content": "对方先改了正文，这里加了两句。", "base_updated_at": base})
    assert r1.status_code == 200

    # 我带着已经过期的 base 再存 → 拦下，服务端整份随 detail 回来
    r2 = await client.patch(f"/studio/nodes/{n['id']}", json={
        "content": "我页面上写的这一段还没存过。", "base_updated_at": base})
    assert r2.status_code == 409
    d = r2.json()["detail"]
    assert d["conflict"] is True
    assert d["server"]["content"] == "对方先改了正文，这里加了两句。"
    assert d["server"]["word_count"] > 0
    assert d["server"]["updated_at"] > base

    # 人在冲突条里点了「就存我这份」：带放行重发，照常落库
    r3 = await client.patch(f"/studio/nodes/{n['id']}", json={
        "content": "我页面上写的这一段还没存过。",
        "base_updated_at": base, "ignore_conflict": True})
    assert r3.status_code == 200
    got = await store.get_node(n["id"])
    assert got["content"] == "我页面上写的这一段还没存过。"

    # 成功响应里就是新版本的时间戳：拿它当 base 的下一笔正常放行
    r4 = await client.patch(f"/studio/nodes/{n['id']}", json={
        "content": "顺着又写了一段新的。", "base_updated_at": r3.json()["updated_at"]})
    assert r4.status_code == 200


async def test_no_base_and_title_only_pass(studio) -> None:
    """不带 base 的旧调用与只改标题的写入都不拦——冲突闸只管正文。"""
    client, store = studio
    n = await make_node(store)
    # 先让库里变新
    await store.update_node(n["id"], content="别处写进去的正文内容。")

    r = await client.patch(f"/studio/nodes/{n['id']}", json={"content": "旧调用没有 base。"})
    assert r.status_code == 200

    # 天价旧的 base 改标题：没动正文就不该被拦（改状态/摘要同理）
    r = await client.patch(f"/studio/nodes/{n['id']}", json={
        "title": "改了个标题", "base_updated_at": 1.0})
    assert r.status_code == 200
    assert (await store.get_node(n["id"]))["content"] == "旧调用没有 base。"


async def test_conflict_gate_runs_before_final_gate(studio) -> None:
    """定稿也盖不过未见的冲突：先 409 冲突，带 force 定稿也是同一顺序。"""
    client, store = studio
    n = await make_node(store)
    base = n["updated_at"]
    await store.update_node(n["id"], content="另一窗口写的正文。")

    r = await client.patch(f"/studio/nodes/{n['id']}", json={
        "content": "我要直接定稿。", "status": "final", "base_updated_at": base, "force": True})
    assert r.status_code == 409
    assert r.json()["detail"]["conflict"] is True


async def test_meta_endpoint_shape(studio) -> None:
    """轻接口只回五个字段：轮询探冲突不该拖着全文跑。"""
    client, store = studio
    n = await make_node(store)

    r = await client.get(f"/studio/nodes/{n['id']}/meta")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"id", "title", "word_count", "status", "updated_at"}
    assert j["updated_at"] == n["updated_at"]

    r = await client.get("/studio/nodes/n_missing/meta")
    assert r.status_code == 404
