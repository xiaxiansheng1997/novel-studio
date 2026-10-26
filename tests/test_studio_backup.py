"""删除要有退路：删之前自动落一份备份，以及一份把那一节找回来的入口。

`node_versions` 是 `REFERENCES nodes(id) ON DELETE CASCADE`，`delete_node` 又按 path
前缀物理删整棵子树 —— 删卷会连带抹掉该卷每一章的全部历史版本，而库里没有回收站。
`delete_work` 同样什么都没有。也就是说「选中 → 删除 → 确认」这三次点击之内
没有任何一步能反悔，而这是用户的稿子。

不做软删（`status='trashed'`）：那会污染 `list_tree` / `list_readable` / `stats` /
assembler 等所有查询，风险远大于收益。改成删之前把内容落到文件里：
节点备份一份 JSON（含正文与全部版本），整库副本一份 `.sqlite`。

一条底线：**备份写成就该拒绝删除**（宁可删不掉，不能删了找不回），
所以这一份落盘放在删除动作之前，失败直接抛 `BackupError`。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import BackupError, StudioStore

A = "第一章正文：雪落了满山。" * 30


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def one_node(store) -> dict:
    work = await store.create_work(kind="novel", title="备份测试")
    return await store.create_node(work_id=work["id"], kind="chapter", title="第一章",
                                   content="", ordinal=1)


async def with_versions(store, node_id: str, texts: list[str]) -> None:
    for t in texts:
        assert await store.update_node(node_id, content=t, snapshot=True)


def bundle_files(store) -> list:
    return sorted(store.backup_dir("nodes").glob("*.json"))


def read_bundle(store, name: str) -> dict:
    return json.loads((store.backup_dir("nodes") / name).read_text(encoding="utf-8"))


# ==================== 节点备份 ====================

async def test_delete_node_writes_bundle_with_content_and_versions(store) -> None:
    """删一节 → 备份 JSON 里有正文，也有它的全部历史版本（级联删掉就什么都没了）。"""
    node = await one_node(store)
    await with_versions(store, node["id"], ["第一版正文", "第二版正文"])

    assert await store.delete_node(node["id"]) is True

    files = bundle_files(store)
    assert len(files) == 1
    bundle = json.loads(files[0].read_text(encoding="utf-8"))
    assert bundle["node_id"] == node["id"] and bundle["work_id"] == node["work_id"]
    assert bundle["title"] == "第一章" and bundle["ordinal"] == 1
    root = bundle["nodes"][0]
    assert root["content"] == "第二版正文"
    assert [v["content"] for v in root["versions"]] == ["第一版正文", "第二版正文"]
    assert [v["source"] for v in root["versions"]] == ["human", "human"]
    # 文件名带上作品与节点 id：同一秒删两节也不会写到同一个文件里互相盖掉
    assert files[0].name.startswith(f"{node['work_id']}__{node['id']}__")


async def test_restore_node_brings_back_title_body_and_versions(store) -> None:
    node = await one_node(store)
    await with_versions(store, node["id"], ["甲版", "乙版", "丙版"])
    await store.delete_node(node["id"])
    name = bundle_files(store)[0].name

    r = await store.restore_node_backup(name)

    assert r is not None and len(r["restored"]) == 1
    back = await store.get_node(r["root_id"])
    assert back["title"] == "第一章" and back["content"] == "丙版"
    assert back["work_id"] == node["work_id"] and back["ordinal"] == 1
    versions = await store.list_versions(back["id"])
    assert len(versions) == 3
    assert [v["chars"] for v in versions] == [2, 2, 2]
    assert await store.orphan_counts() == {}, "恢复不该留下指向不存在行的引用"


async def test_restore_twice_makes_two_independent_nodes(store) -> None:
    """同一份备份点两次 = 两节都在，各自有各自的后续修改。

    这条决定了恢复时必须发新 id：沿用旧 id 第二次会撞 PRIMARY KEY，
    而且两节共用一个 id 的话，改其中一节会把另一节的版本历史带脏。
    """
    node = await one_node(store)
    await with_versions(store, node["id"], ["唯一定稿"])
    await store.delete_node(node["id"])
    name = bundle_files(store)[0].name

    first = await store.restore_node_backup(name)
    second = await store.restore_node_backup(name)

    assert first["root_id"] != second["root_id"]
    assert len(await store.list_tree(node["work_id"])) == 2
    assert len(await store.list_versions(second["root_id"])) == 1
    # 两节各写各的：改第二节的稿子不会连带第一节的正文与版本历史
    assert await store.update_node(second["root_id"], content="第二节的后续改动",
                                   snapshot=True)
    assert (await store.get_node(first["root_id"]))["content"] == "唯一定稿"
    assert len(await store.list_versions(first["root_id"])) == 1
    assert len(await store.list_versions(second["root_id"])) == 2


async def test_volume_backup_covers_whole_subtree(store) -> None:
    """删卷：bundle 里要有整棵子树，恢复后卷还在、章挂回卷下、层级与父子关系正确。"""
    work = await store.create_work(kind="novel", title="带卷的书", scale="series")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    c1 = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                 title="第一章", content="雪落了满山。" * 20, ordinal=1)
    await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                            title="第二章", content="灯亮到天明。" * 20, ordinal=2)
    await store.create_node(work_id=work["id"], kind="section", parent_id=c1["id"],
                            title="第一节", content="风从窗缝里进来。" * 20, ordinal=3)
    await store.delete_node(vol["id"])

    bundle = read_bundle(store, bundle_files(store)[0].name)
    assert len(bundle["nodes"]) == 4
    assert {n["title"] for n in bundle["nodes"]} == {"第一卷", "第一章", "第二章", "第一节"}

    r = await store.restore_node_backup(bundle_files(store)[0].name)

    assert r is not None and len(r["restored"]) == 4
    rows = {n["title"]: n for n in await store.list_tree(work["id"])}
    assert rows["第一卷"]["parent_id"] is None and rows["第一卷"]["depth"] == 0
    assert rows["第一章"]["parent_id"] == rows["第一卷"]["id"]
    assert rows["第二章"]["parent_id"] == rows["第一卷"]["id"]
    assert rows["第一节"]["parent_id"] == rows["第一章"]["id"]
    assert rows["第一节"]["path"].startswith(rows["第一章"]["path"] + ".")
    assert rows["第一节"]["depth"] == 2
    # 章号追加到末尾，原来的 1/2/3 让给书里现存的章节；顺序靠上下移挪回去
    assert [n["ordinal"] for n in await store.list_readable(work["id"])] == [1, 2, 3]
    assert (await store.get_node(rows["第二章"]["id"]))["content"] == "灯亮到天明。" * 20
    assert await store.orphan_counts() == {}


async def test_restore_without_original_parent_goes_to_root(store) -> None:
    """原父节点也没了（先删章、再删它所在的卷）：挂到作品根，不报错、不留孤儿。"""
    work = await store.create_work(kind="novel", title="先删章后删卷", scale="series")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    ch = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                 title="第一章", content="稿子。" * 30, ordinal=1)
    await store.delete_node(ch["id"])
    name = bundle_files(store)[0].name
    await store.delete_node(vol["id"])
    assert await store.list_tree(work["id"]) == []

    r = await store.restore_node_backup(name)

    assert r is not None
    assert r["restored"][0]["hung_on_root"] is True
    node = await store.get_node(r["root_id"])
    assert node["parent_id"] is None and node["depth"] == 0
    assert node["title"] == "第一章" and node["ordinal"] == 1
    assert await store.orphan_counts() == {}


async def test_restore_rejects_a_work_that_is_gone(store) -> None:
    """整本书都删了：节点备份没法单独挂回来（那种情况只能走整库副本）。"""
    node = await one_node(store)
    await store.delete_node(node["id"])
    name = bundle_files(store)[0].name
    assert await store.delete_work(node["work_id"]) is True

    assert await store.restore_node_backup(name) is None
    assert store.list_node_backups()[0]["title"] != ""  # 备份还在，只是挂不回去
    assert await store.restore_node_backup("w_x__n_y__19700101_000000.json") is None


# ==================== 整库副本与保留策略 ====================

async def test_delete_work_copies_the_database(store) -> None:
    work = await store.create_work(kind="novel", title="要整本删掉的书")
    await store.create_node(work_id=work["id"], kind="chapter", title="第一章",
                            content="正文。" * 50, ordinal=1)
    live_size = store.db_path.stat().st_size

    assert await store.delete_work(work["id"]) is True

    copies = sorted(store.backup_dir("works").glob("*.sqlite"))
    assert len(copies) == 1
    assert copies[0].name.startswith(f"studio-before-delete-work-{work['id']}-")
    assert copies[0].stat().st_size >= live_size, "副本是删除之前的库，只会更大不会更小"
    # 副本目录之外的那个当前库本体不在保留策略的射程里，绝不能被清理掉
    assert store.db_path.is_file()


async def test_prune_backups_keeps_only_the_newest(store) -> None:
    """备份不能堆盘：只留最近 N 个，而且不动当前库。"""
    node = await one_node(store)
    names = []
    for i in range(4):
        n = await store.create_node(work_id=node["work_id"], kind="chapter",
                                    title=f"第{i}章", content="字。" * 10, ordinal=i + 2)
        await store.delete_node(n["id"])
        # 文件名里带节点 id，用它认领自己刚写的那一份（文件名里的时间戳只到秒）
        names.append(next(p.name for p in bundle_files(store) if n["id"] in p.name))
    assert len(bundle_files(store)) == 4
    # 同一秒里删掉的四份，时间戳分不出先后；把 mtime 拉开才能验证「留最新」这件事
    base = time.time()
    for i, name in enumerate(names):
        os.utime(store.backup_dir("nodes") / name, (base + i, base + i))

    gone = store.prune_backups("nodes", 2)

    assert sorted(gone) == sorted(names[:2])
    assert sorted(p.name for p in bundle_files(store)) == sorted(names[2:])
    assert store.db_path.is_file()


async def test_backup_dir_is_created_on_demand(tmp_path) -> None:
    """备份目录不存在时自动建出来：不能因为没人 `mkdir` 就把删除动作卡死。"""
    db = tmp_path / "sub" / "studio.db"
    s = StudioStore(db)
    await s.init()
    try:
        assert not (db.parent / "backups").exists()
        node = await s.create_node(work_id=(await s.create_work(kind="novel", title="新目录"))["id"],
                                   kind="chapter", title="第一章", content="稿", ordinal=1)
        await s.delete_node(node["id"])
        assert (db.parent / "backups" / "nodes").is_dir()
        assert len(bundle_files(s)) == 1
    finally:
        await s.close()


async def test_failed_backup_refuses_the_delete(store, monkeypatch) -> None:
    """备份写不成 → 拒绝删除并报错。这一条是本项的取舍：宁可删不掉。"""
    node = await one_node(store)
    await with_versions(store, node["id"], ["不能丢的稿子"])

    def boom(kind: str):
        raise BackupError("磁盘写不进去（测试注入）")

    monkeypatch.setattr(store, "_backup_dir", boom)
    with pytest.raises(BackupError):
        await store.delete_node(node["id"])

    assert await store.get_node(node["id"]) is not None, "备份失败时一个字都不能少"
    assert len(await store.list_versions(node["id"])) == 1


async def test_delete_work_refuses_when_copy_fails(store, monkeypatch) -> None:
    """整库副本同理：`shutil.copy2` 抛 OSError 要翻成 BackupError，库里的书保持原样。"""
    work = await store.create_work(kind="novel", title="不让删的书")
    monkeypatch.setattr(StudioStore, "_stamp",
                        lambda self: (_ for _ in ()).throw(OSError("盘坏了")))

    with pytest.raises(BackupError):
        await store.delete_work(work["id"])

    assert await store.get_work(work["id"]) is not None


# ==================== 接口层冒烟 ====================

def test_backup_routes_list_and_restore(tmp_path) -> None:
    """`GET /studio/backups` 与 `POST /studio/backups/{name}/restore` 走得通。

    本轮不做恢复 UI，但接口必须先验证能得到：前端下一轮接上时不必重新踩一遍。
    全程走 HTTP：aiosqlite 的连接绑在建立它的那个事件循环上。
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
        work = client.post("/studio/works", json={
            "kind": "novel", "title": "备份接口", "scale": "single"}).json()
        node = client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "chapter", "title": "第一章", "content": A}).json()

        assert client.get("/studio/backups").json()["nodes"] == [], "没删过就不该有备份"
        assert client.delete(f"/studio/nodes/{node['id']}").status_code == 200

        rows = client.get("/studio/backups").json()
        assert len(rows["nodes"]) == 1
        name = rows["nodes"][0]["name"]
        assert rows["nodes"][0]["title"] == "第一章" and rows["nodes"][0]["node_count"] == 1

        back = client.post(f"/studio/backups/{name}/restore")
        assert back.status_code == 200
        restored = back.json()["root_id"]
        assert client.get(f"/studio/nodes/{restored}").json()["content"] == A

        # 路径穿越与整库副本都不受理；不存在的那份给 404
        assert client.post("/studio/backups/..%2Fstudio.sqlite/restore").status_code in (400, 404)
        assert client.post("/studio/backups/x.json/restore").status_code == 404
