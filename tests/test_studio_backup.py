"""删除要有退路：删之前自动落一份备份，以及一份把那一节找回来的入口。

`node_versions` 是 `REFERENCES nodes(id) ON DELETE CASCADE`，`delete_node` 又按 path
前缀物理删整棵子树 —— 删卷会连带抹掉该卷每一章的全部历史版本，而库里没有回收站。
`delete_work` 同样什么都没有。也就是说「选中 → 删除 → 确认」这三次点击之内
没有任何一步能反悔，而这是用户的稿子。

不做软删（`status='trashed'`）：那会污染 `list_tree` / `list_readable` / `stats` /
assembler 等所有查询，风险远大于收益。改成删之前把内容落到文件里：
节点备份一份 JSON（含正文与全部版本），整库副本一份 `.sqlite`。

一条底线：**备份写不成才该中止删除**（宁可删不掉，不能删了找不回），
所以这一份落盘放在删除动作之前，失败直接抛 `BackupError`。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import BackupError, StudioStore, check_node_bundle

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
    assert await store.delete_work(work["id"]) is True

    copies = sorted(store.backup_dir("works").glob("*.sqlite"))
    assert len(copies) == 1
    assert copies[0].name.startswith(f"studio-before-delete-work-{work['id']}-")
    # 这份文件唯一的用处是「出事时盖回去」，所以验的是它能不能用，而不是它有多大：
    # VACUUM INTO 产出的是压实过的新库，体积比带着空闲页的当前库小是完全正常的
    con = sqlite3.connect(copies[0])
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        got = con.execute("SELECT title, content FROM nodes").fetchall()
    finally:
        con.close()
    assert [g[0] for g in got] == ["第一章"], "副本里应当有被删掉的那一章"
    assert got[0][1] == "正文。" * 50, "稿子内容必须与删除前一字不差"
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
    """整库副本同理：取时间戳或写副本出错要翻成 BackupError，库里的书保持原样。"""
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

        # 第一道闸：`%2F` 解码后多出路径段，路由的 `[^/]+` 不匹配 → 请求进不了 handler
        assert client.post("/studio/backups/..%2Fstudio.sqlite/restore").status_code == 404
        # 第二道闸：反斜杠不切路径段，能走到 handler，由文件名校验拦下（大小写扩展名也一样）
        for bad in ("..%5C..%5Cstudio.db", "..%5Cx.json", "x.JSON"):
            assert client.post(f"/studio/backups/{bad}/restore").status_code == 400, bad
        # 名字合法但没有这一份备份
        assert client.post("/studio/backups/x.json/restore").status_code == 404
        # 界面上不该看到本机路径
        listing = client.get("/studio/backups").json()["dir"]
        assert not os.path.isabs(listing) and str(tmp_path) not in listing, listing


async def test_backup_covers_exactly_the_subtree_that_got_deleted(store, monkeypatch) -> None:
    """备份里的子树必须与真删掉的那一套完全一致，插队挤进来的那节也不例外。

    这条盯的是「快照与删除分两次持锁」那个写法：锁一放，已经在等锁的 `create_node`
    就能挤进这棵子树里，接着被 path 前缀连带删掉 —— 备份里没有它，等于删了还宣称备份过。
    这里直接盯住快照这个接缝（它现在与删除同在一次持锁里），并把删除真正带走的 id 抓下来对账。
    """
    work = await store.create_work(kind="novel", title="并发删", scale="series")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    ch = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                 title="第一章", content="雪。" * 20, ordinal=1)
    before = {n["id"] for n in await store.list_tree(work["id"])}

    original = store._node_backup_bundle_locked
    drop = store._drop_unit_rows
    started: list = []
    removed: list[str] = []

    async def spy(node_id: str):
        bundle = await original(node_id)
        # 快照已经拿到手、删除还没开始：放一个「往这棵子树里加一节」过来抢同一把锁
        started.append(asyncio.create_task(store.create_node(
            work_id=work["id"], kind="chapter", parent_id=ch["id"],
            title="插队的一节", content="不该被吃掉。" * 10, ordinal=2)))
        for _ in range(2):
            await asyncio.sleep(0)   # 让它跑到取锁处挂住
        return bundle

    async def spy_drop(ref_kind: str, ref_ids: list[str]):
        # 这次删除真正带走的那几个 id。验的是「带走的每一节都在备份里」，
        # 只比备份与删之前的快照相等是抓不住的：挤进来的那一节两边都不在
        if ref_kind == "node":
            removed.extend(ref_ids)
        return await drop(ref_kind, ref_ids)

    monkeypatch.setattr(store, "_node_backup_bundle_locked", spy)
    monkeypatch.setattr(store, "_drop_unit_rows", spy_drop)
    assert await store.delete_node(vol["id"]) is True

    if started:
        try:
            await started[0]
        except ValueError:
            pass   # 新写法下它只能排在删除之后：父节点已经没了，创建直接被拒
    backed = {n["id"] for n in read_bundle(store, bundle_files(store)[0].name)["nodes"]}
    assert backed == before, "备份应当正好是删之前那棵子树，不多不少"
    assert set(removed) == backed, "被这次删除带走的那一节，备份里必须都有"
    assert await store.orphan_counts() == {}


async def test_restore_marks_upstream_summaries_stale(store) -> None:
    """找回一节 = 库里多出整段正文：卷摘要与全书摘要必须重新判过期。

    不判的话它们还停留在「没有这一节」的那一版上却自称新鲜：assembler 的 S3 照原样注入，
    右栏「N 节摘要待重算」也看不见 —— 那正是第 1 条刚堵死的那类假记忆，别再开一条口子。
    """
    from app.studio import indexer

    work = await store.create_work(kind="novel", title="找回要过期", scale="series")
    vol = await store.create_node(work_id=work["id"], kind="volume", title="第一卷")
    ch = await store.create_node(work_id=work["id"], kind="chapter", parent_id=vol["id"],
                                 title="第一章", content="雪落了满山。" * 20, ordinal=1)
    await store.delete_node(ch["id"])
    # 这一卷在「少了那一章」之后重算过，指纹与当时的内容是一致的
    await store.upsert_summary(work["id"], scope="container", ref_id=vol["id"],
                               content="这一卷是空的", ref_hash="hash-at-the-time")
    await store.upsert_summary(work["id"], scope="work", ref_id=work["id"],
                               content="全书是空的", ref_hash="hash-at-the-time")
    assert (await store.get_summary("container", vol["id"]))["fresh"], "重算过就该是新鲜的"

    assert await store.restore_node_backup(bundle_files(store)[0].name) is not None

    assert not (await store.get_summary("container", vol["id"]))["fresh"], \
        "章找回来了，卷摘要不能还自称新鲜"
    assert not (await store.get_summary("work", work["id"]))["fresh"]
    status = await indexer.index_status(store, work["id"])
    assert status["stale_upstream"] == 2, "过期计数要在右栏看得见，不然人不知道该重算"
    # 结构变了、内容没变：不重算索引，但要把库标脏（口径与上下移那条一致）
    assert (await store.index_state(work["id"]))["dirty"] == 1


async def test_failed_backup_leaves_no_half_file(store, monkeypatch) -> None:
    """写失败不能留下半截 JSON：它 mtime 最新，会被保留策略当成「最近一份」留在盘上。"""
    node = await one_node(store)

    def boom(self, *args, **kwargs):
        raise OSError("盘满了")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(BackupError):
        await store.delete_node(node["id"])

    assert [q.name for q in store.backup_dir("nodes").iterdir()] == []
    assert await store.get_node(node["id"]) is not None, "备份失败时一个字都不能少"


async def test_prune_failure_does_not_block_delete(store, monkeypatch) -> None:
    """清理旧备份是记账，不该有拦住删除的权力：它失败也不能让删除失败。"""
    node = await one_node(store)

    def boom(kind: str, keep: int):
        raise OSError("这个目录扫不动")

    monkeypatch.setattr(store, "prune_backups", boom)
    assert await store.delete_node(node["id"]) is True
    assert len(bundle_files(store)) == 1


async def test_restore_rejects_a_bundle_that_is_not_a_dict(store) -> None:
    """人手工改过的备份可能是数组、字符串或 `nodes` 不是列表：读不懂就返回 None，不能抛异常。"""
    node = await one_node(store)
    await store.delete_node(node["id"])
    folder = store.backup_dir("nodes")
    good = bundle_files(store)[0]

    for i, text in enumerate(('[1, 2]', '"just a string"',
                              '{"work_id": "w_1", "nodes": {"a": 1}}')):
        name = f"broken{i}__{good.name}"
        (folder / name).write_text(text, encoding="utf-8")
        assert await store.restore_node_backup(name) is None, name

    assert await store.list_tree(node["work_id"]) == [], "坏备份不该往库里塞出半个节点"
    rows = {r["name"]: r for r in store.list_node_backups()}
    assert rows[good.name]["node_count"] == 1, "坏文件混在列表里也不能把好的那份挤掉"


def test_route_reports_backup_failure_without_local_paths(tmp_path) -> None:
    """备份失败的 500 只回固定文案：`detail` 会被前端直接显示，不能把本机路径说出去。"""
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
            "kind": "novel", "title": "备份失败", "scale": "single"}).json()
        node = client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "chapter", "title": "第一章", "content": A}).json()

        def boom(bundle):
            raise BackupError(f"备份目录不可用 {store.backup_dir('nodes')}: 权限不够")

        store._write_node_backup = boom
        denied = client.delete(f"/studio/nodes/{node['id']}")
        assert denied.status_code == 500
        assert str(tmp_path) not in denied.json()["detail"], "界面不该看到本机路径"
        assert client.get(f"/studio/nodes/{node['id']}").json()["content"] == A

        async def boom_copy(label: str) -> None:
            raise BackupError(f"整库副本没写成：{store.db_path}")

        store._copy_db_backup = boom_copy
        denied2 = client.delete(f"/studio/works/{work['id']}")
        assert denied2.status_code == 500
        assert str(tmp_path) not in denied2.json()["detail"]
        assert client.get(f"/studio/works/{work['id']}").status_code == 200


async def test_prune_only_counts_real_backups(store) -> None:
    """.part 与顺手丢进这个目录的东西都不该占用保留名额。

    半截文件是写坏了或进程被杀留下的，mtime 最新：算进「留最近 N 份」就会把一份
    真备份挤出去——那份「最近」恰恰是人最可能需要的那一份。反过来，不属于备份的
    文件更不该被清理当成旧备份删掉。
    """
    node = await one_node(store)
    real = []
    for i in range(3):
        n = await store.create_node(work_id=node["work_id"], kind="chapter",
                                    title=f"第{i}章", content="字。" * 10, ordinal=i + 2)
        await store.delete_node(n["id"])
        real.append(next(p for p in bundle_files(store) if n["id"] in p.name))
    assert len(real) == 3
    folder = store.backup_dir("nodes")
    base = time.time()
    for i, p in enumerate(real):
        os.utime(p, (base + i, base + i))
    half = folder / f"{real[-1].name}.part"      # 比最新一份还新的半截文件
    half.write_text('{"work_id": "w_1", "nodes": [', encoding="utf-8")
    os.utime(half, (base + 99, base + 99))
    note = folder / "note.txt"                   # 不是备份的东西
    note.write_text("顺手丢进来的", encoding="utf-8")
    os.utime(note, (base + 50, base + 50))

    gone = store.prune_backups("nodes", 1)

    assert sorted(gone) == sorted(p.name for p in real[:2]), gone
    assert real[2].is_file(), "最新的那一份真备份不该被半截文件挤出名额"
    assert half.is_file() and note.is_file(), "名单之外的文件轮不到清理来删"
    assert [r["name"] for r in store.list_node_backups()] == [real[2].name], \
        "列举与清理必须认同一套名单"""


async def test_the_script_and_restore_share_one_bundle_check(store) -> None:
    """脚本替恢复先校验结构，两处的判据必须是同一个来源。

    `peek` 原来只查「顶层是对象 + nodes 是列表」，比 `restore_node_backup` 的拒绝
    条件松：`nodes` 是空的、条目不是对象、没写 work_id 都能放行，于是真恢复时
    `restore` 返回 None，脚本却报「这本书已经整本删除」——本轮要消灭的就是这种谎。
    """
    from scripts.restore_backup import peek

    node = await one_node(store)
    await store.delete_node(node["id"])
    good = bundle_files(store)[0]
    valid = json.loads(good.read_text(encoding="utf-8"))
    assert check_node_bundle(valid) == ""
    assert peek(store, good.name) is not None

    broken = [
        {**valid, "nodes": []},
        {**valid, "nodes": ["不是对象"]},
        {k: v for k, v in valid.items() if k != "work_id"},
        {**valid, "nodes": {"a": 1}},
    ]
    folder = store.backup_dir("nodes")
    before = len(await store.list_tree(node["work_id"]))
    for i, bad in enumerate(broken):
        name = f"odd{i}__{good.name}"
        (folder / name).write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        assert check_node_bundle(bad), name
        assert peek(store, name) is None, f"脚本没拦住：{name} —— {check_node_bundle(bad)}"
        assert await store.restore_node_backup(name) is None, name

    assert len(await store.list_tree(node["work_id"])) == before, "坏备份一个节点都不该进库"
