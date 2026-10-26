"""版本历史要能读回来、点得回去。

存这一份历史其实一直都在做（每次「采用」与生成落库都 `snapshot=True`），
问题是**只有写没有读**：`list_versions` 只返回字数，没有读正文的接口，也没有回滚，
前端一处都没引用。更糟的是 `source` 硬编码成 `'human'`，`run_id` 建了列没人写
—— 于是 AI 生成的那一版和用户手敲的那一版在表里长得一模一样，
就算把历史页做出来，作者也分不清哪条是自己在改剧情、哪条是模型抽风。

所以这一条同时补三样：来源标注、读回正文、回滚。回滚的口径只有一条底线——
**不能吃掉用户当下没保存的改动**：自动保存只写 `nodes` 不写版本，
改到一半点回滚，必须先把这份正文落成一版再回，否则那是删稿不是回滚。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import summarizer
from app.studio.store import StudioStore

A = "第一版：她推门进去，屋里没人。" * 20
B = "第二版：她推门进去，屋里坐着三个人。" * 20
C = "第三版：她没推门，转身走了。" * 20


class FakeGateway:
    """只记账：摘要重算了几次，这里就是几次。全程不打网络。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        self.calls.append(prompt)
        return SimpleNamespace(text=f"记忆卡 #{len(self.calls)}")

    @property
    def calls_n(self) -> int:
        return len(self.calls)


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def one_node(store) -> dict:
    work = await store.create_work(kind="novel", title="版本测试")
    return await store.create_node(work_id=work["id"], kind="chapter",
                                   title="第一章", content="", ordinal=1)


# ==================== 来源标注 ====================

async def test_sources_do_not_mix(store) -> None:
    """三条来路各存一版，历史页要分得清是谁改的、能不能追到哪一次生成。"""
    node = await one_node(store)
    await store.update_node(node["id"], content=A, snapshot=True)               # PATCH 走默认 human
    await store.update_node(node["id"], content=B, snapshot=True,
                            snapshot_source="ai_draft", run_id="r_1")
    await store.update_node(node["id"], content=C, snapshot=True,
                            snapshot_source="ai_polish")

    rows = await store.list_versions(node["id"])
    assert [(r["source"], r["run_id"]) for r in rows] == [
        ("ai_polish", None), ("ai_draft", "r_1"), ("human", None)]
    # 列表只给字数，正文得靠 get_version 读回来（以前到这里就断了）
    assert rows[0]["chars"] == len(C)
    assert (await store.get_version(rows[0]["id"]))["content"] == C


# ==================== 回滚语义 ====================

async def test_restore_keeps_the_unsaved_edit(store) -> None:
    """当下这份还没进版本表的改动，必须被回滚保住，而不是被目标版本盖掉。"""
    node = await one_node(store)
    await store.update_node(node["id"], content=A, snapshot=True)
    await store.update_node(node["id"], content=B)  # 模拟自动保存：不 snapshot
    target = (await store.list_versions(node["id"]))[0]["id"]

    back = await store.restore_version(node["id"], target)

    assert back["content"] == A
    rows = await store.list_versions(node["id"])
    assert len(rows) == 2, "回滚前应该先落一条当下正文"
    assert rows[0]["source"] == "human"
    assert (await store.get_version(rows[0]["id"]))["content"] == B


async def test_restore_does_not_duplicate_versions(store) -> None:
    """已经存过的正文不重复塞：历史页里连着两条一模一样的行，只会让人看不出改了什么。"""
    node = await one_node(store)
    await store.update_node(node["id"], content=A, snapshot=True)
    await store.update_node(node["id"], content=B, snapshot=True)
    old = (await store.list_versions(node["id"]))[-1]["id"]

    back = await store.restore_version(node["id"], old)

    assert back["content"] == A
    assert len(await store.list_versions(node["id"])) == 2
    # 回滚本身就是把某一版重新变成当前正文，不需要再造一份
    assert (await store.get_version(old))["content"] == A


async def test_restore_to_current_version_is_a_no_op(store) -> None:
    node = await one_node(store)
    await store.update_node(node["id"], content=A, snapshot=True)
    await store.update_node(node["id"], content=B, snapshot=True)
    newest = (await store.list_versions(node["id"]))[0]["id"]

    back = await store.restore_version(node["id"], newest)

    assert back["content"] == B
    assert len(await store.list_versions(node["id"])) == 2


async def test_restore_rejects_another_nodes_version(store) -> None:
    """版本 id 是外来的（别的节点、甚至已删节点）就直接拒绝，不能把正文写到这个节点上。"""
    mine = await one_node(store)
    other = await one_node(store)
    await store.update_node(other["id"], content=B, snapshot=True)
    foreign = (await store.list_versions(other["id"]))[0]["id"]
    await store.update_node(mine["id"], content=A, snapshot=True)

    assert await store.restore_version(mine["id"], foreign) is None
    assert (await store.get_node(mine["id"]))["content"] == A


async def test_restore_rolls_the_memories_back_too(store) -> None:
    """回滚也是改正文，摘要的失效传播必须跟上——不然这条会把第 1 条的修复整个绕过去。"""
    gw = FakeGateway()
    node = await one_node(store)
    await store.update_node(node["id"], content=A, snapshot=True)
    await summarizer.summarize_node(gw, store, await store.get_node(node["id"]), model="m")
    await store.update_node(node["id"], content=B)
    await summarizer.summarize_node(gw, store, await store.get_node(node["id"]), model="m")
    assert gw.calls_n == 2

    old = (await store.list_versions(node["id"]))[0]["id"]
    await store.restore_version(node["id"], old)

    assert not (await store.get_summary("node", node["id"]))["fresh"]
    assert await store.stale_summaries(node["work_id"])
    # 回到 A 之后重算，拿到的才是 A 这一版的摘要
    assert await summarizer.summarize_node(gw, store, await store.get_node(node["id"]),
                                          model="m") == "记忆卡 #3"
    assert (await store.get_summary("node", node["id"]))["fresh"]


# ==================== 接口层冒烟 ====================

def test_version_routes_read_and_restore(tmp_path) -> None:
    """两个新接口：读回一版正文、回滚到那一版。不存在的 version_id 一律 404。

    这里全程走 HTTP：aiosqlite 的连接绑在建立它的那个事件循环上，
    用 TestClient 时不能让 store 在 pytest 的循环里初始化。
    """
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

    with TestClient(app) as client:
        work = client.post("/studio/works", json={"kind": "novel", "title": "路由", "scale": "single"}).json()
        node = client.post(f"/studio/works/{work['id']}/nodes",
                           json={"kind": "chapter", "title": "第一章", "content": ""}).json()
        base = f"/studio/nodes/{node['id']}"
        for text in (A, B):
            assert client.patch(base, json={"content": text, "snapshot": True}).status_code == 200

        versions = client.get(f"{base}/versions").json()
        assert len(versions) == 2
        first_id = versions[-1]["id"]

        got = client.get(f"{base}/versions/{first_id}")
        assert got.status_code == 200 and got.json()["content"] == A
        assert client.get(f"{base}/versions/v_没有这一版").status_code == 404

        back = client.post(f"{base}/versions/{first_id}/restore")
        assert back.status_code == 200 and back.json()["content"] == A
        assert client.post(f"{base}/versions/v_没有这一版/restore").status_code == 404
        # 回滚后当前正文真的是 A
        assert client.get(base).json()["content"] == A
