"""定稿闸门：这一节还有未处理的硬告警时，定稿要先过一道确认。

两条入口共用同一份判断：
- `GET /studio/nodes/{id}/final-gate` 只读，前端拿它把「哪几条」摆进 confirm；
- `PATCH /studio/nodes/{id}` 带 `status=final` 时拦一道 409，带 `force=true` 放行。

边界要钉住：只有本节、只有 error、只有未处理才算拦——warn/info 是「八成有问题」，
全书级告警（node_id 为空）不挡单独一节，点过「已处理」的也不该再挡。
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


async def make_chapter(store, title: str = "第一章") -> dict:
    work = await store.create_work(kind="novel", title="闸门测试", scale="series")
    node = await store.create_node(work_id=work["id"], kind="chapter", title=title)
    return node


async def set_final(client, node_id: str, *, force: bool = False):
    return await client.patch(
        f"/studio/nodes/{node_id}",
        json={"status": "final", **({"force": True} if force else {})},
    )


# ==================== 拦与放 ====================

async def test_open_error_blocks_final(studio) -> None:
    """有未处理的硬告警：409 带条数与规则，状态一个字都不改。"""
    client, store = studio
    node = await make_chapter(store)
    await store.add_issue(node["work_id"], rule="dead_character",
                          message="阿泠已经死了，这一节又说话", node_id=node["id"],
                          severity="error")

    r = await set_final(client, node["id"])

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["open_errors"] == 1
    assert detail["issues"][0]["rule"] == "dead_character"
    assert (await store.get_node(node["id"]))["status"] == "draft"


async def test_force_lets_it_through_and_keeps_issue(studio) -> None:
    """人确认之后带 force：定稿成功；告警不跟着消——它要人去「告警」里点已处理。"""
    client, store = studio
    node = await make_chapter(store)
    await store.add_issue(node["work_id"], rule="dead_character", message="人已故",
                          node_id=node["id"], severity="error")

    r = await set_final(client, node["id"], force=True)

    assert r.status_code == 200
    assert r.json()["status"] == "final"
    open_rows = await store.list_issues(node["work_id"], node_id=node["id"], only_open=True)
    assert [x["rule"] for x in open_rows] == ["dead_character"]


async def test_warn_and_info_do_not_block(studio) -> None:
    """提醒与提示只是提示：直接定稿，不弹确认。"""
    client, store = studio
    node = await make_chapter(store)
    await store.add_issue(node["work_id"], rule="structure_incomplete", message="少两节",
                          node_id=node["id"], severity="warn")
    await store.add_issue(node["work_id"], rule="phrase_repeat", message="重复用词",
                          node_id=node["id"], severity="info")

    r = await set_final(client, node["id"])

    assert r.status_code == 200
    assert r.json()["status"] == "final"


async def test_resolved_error_does_not_block(studio) -> None:
    """点过「已处理」的硬告警不再拦：不然改完了还定不了稿。"""
    client, store = studio
    node = await make_chapter(store)
    issue = await store.add_issue(node["work_id"], rule="dead_character", message="人已故",
                                  node_id=node["id"], severity="error")
    assert await store.resolve_issue(issue["id"])

    r = await set_final(client, node["id"])

    assert r.status_code == 200


async def test_other_nodes_and_work_level_errors_do_not_block(studio) -> None:
    """别的节的硬告警、全书的硬告警（node_id 为空）都不挡这一节。"""
    client, store = studio
    node = await make_chapter(store, "第一章")
    other = await store.create_node(work_id=node["work_id"], kind="chapter", title="第二章")
    await store.add_issue(node["work_id"], rule="dead_character", message="别节的问题",
                          node_id=other["id"], severity="error")
    await store.add_issue(node["work_id"], rule="foreshadow_overdue", message="伏笔超期",
                          node_id=None, severity="error")

    r = await set_final(client, node["id"])

    assert r.status_code == 200


# ==================== 只读预检 ====================

async def test_final_gate_endpoint_lists_open_errors(studio) -> None:
    """预检接口给前端拼 confirm：条数、规则、原文，超过 10 条标 truncated。"""
    client, store = studio
    node = await make_chapter(store)
    for i in range(12):
        await store.add_issue(node["work_id"], rule=f"rule_{i}", message=f"第 {i} 条",
                              node_id=node["id"], severity="error")
    await store.add_issue(node["work_id"], rule="structure_incomplete", message="提醒不算",
                          node_id=node["id"], severity="warn")

    body = (await client.get(f"/studio/nodes/{node['id']}/final-gate")).json()

    assert body["open_errors"] == 12
    assert len(body["issues"]) == 10
    assert body["truncated"] == 2
    assert all(set(x) == {"id", "rule", "message"} for x in body["issues"])


async def test_final_gate_empty_and_unknown_node(studio) -> None:
    """干净的一节回零；节点不存在 404，不是「零条可以定稿」。"""
    client, store = studio
    node = await make_chapter(store)

    body = (await client.get(f"/studio/nodes/{node['id']}/final-gate")).json()
    assert body == {"open_errors": 0, "issues": [], "truncated": 0}

    r = await client.get("/studio/nodes/n_missing/final-gate")
    assert r.status_code == 404
