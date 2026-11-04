"""整库每日快照：没删过的东西也要多一份副本。

`backups/works/` 那一份只在**删作品**的那一刻写。而真实事故不是误删，是
「写了三个月，库文件坏了 / 磁盘没了 / 被同步盘盖了」——那种情况下盘上一个副本都没有，
当时 README 的「已知限制」自己就写着「没删过的东西不会自己多一份副本，库本身坏了仍然没有退路」。

补法是一句 SQL：`VACUUM INTO`。它不是 `shutil.copy2`——本库是默认的 rollback journal
模式，热拷贝既不参与文件锁协议（大概率把写了一半的页拷进去），也不带 `-journal`
那个回滚文件（拷贝连「坏掉的库」都算不上，是一块拼起来的东西）。`cleanup_works.py`
里那条是维护脚本顺手做，不该被抄成产品代码。

口径与既有那两条一起数：留最近 7 份、只认 `*.sqlite` 这一种后缀、清理失败不拦正事。
区别于删除副本的地方是**三道门**：空库不占名额、没写过字不产重复副本、今天取过不再取。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import (
    BACKUP_SNAPSHOT_KEEP,
    SNAPSHOT_DAY_KEY,
    SNAPSHOT_TS_KEY,
    BackupError,
    StudioStore,
)

BODY = "第一章正文：雪落了满山。" * 30


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


def snap_files(store) -> list:
    return sorted(store.backup_dir("snapshots").glob("*.sqlite"))


async def write_a_book(store, title: str = "快照测试") -> dict:
    """一本书两章：快照的头两道门（有作品 / 写过字）都要真的过得了。"""
    work = await store.create_work(kind="novel", title=title)
    for i, body in enumerate(("雪落了满山。", "灯亮到天明。")):
        await store.create_node(work_id=work["id"], kind="chapter", title=f"第{i + 1}章",
                                content=body * 20, ordinal=i + 1)
    return work


# ==================== 三道门 ====================

async def test_second_call_same_day_takes_nothing(store) -> None:
    """同一天只取一份：循环每半小时醒一次，不能每次都往目录里堆一个相同的东西。"""
    await write_a_book(store)

    first = await store.maybe_daily_snapshot()

    assert first is not None and first.is_file()
    assert first.name.startswith("studio-daily-") and first.suffix == ".sqlite"
    assert await store.maybe_daily_snapshot() is None
    assert len(snap_files(store)) == 1
    # 记的那两本账是给下一轮看的，界面上的设置项不认识它们（也不该让人手工改）
    assert await store.get_setting(SNAPSHOT_DAY_KEY) != ""
    assert await store.get_setting(SNAPSHOT_TS_KEY) != ""


async def test_no_new_writing_makes_no_extra_copy(store) -> None:
    """没写过字就不取：日期那道门放开也没用，第二道门得自己拦住。

    这条防的是「开着服务不动笔，每天照旧多出两份一模一样的库」——那不叫备份，
    那叫把唯一的磁盘名额花在同一个东西上，还顺手把真改动那一份挤出去。
    """
    await write_a_book(store)
    assert await store.maybe_daily_snapshot() is not None

    # 当作隔天早上醒来：昨天的日期那一道门放开，只剩「有没有改动」这一条
    await store.set_setting(SNAPSHOT_DAY_KEY, "2000-01-01")
    assert await store.maybe_daily_snapshot() is None
    assert len(snap_files(store)) == 1, "一个字没改就该一个文件都不产生"

    # 真写了一个字：这一份就该有
    node = (await store.list_readable((await store.list_works())[0]["id"]))[0]
    assert await store.update_node(node["id"], content="新写的一段正文。" * 20)
    assert await store.maybe_daily_snapshot() is not None
    assert len(snap_files(store)) == 2


async def test_empty_library_takes_nothing(store, monkeypatch) -> None:
    """一本作品都没建过的空库不占名额。

    把「最后一次改动」抬到当前时间，是为了单独验这一道门：否则新库那条
    `MAX(updated_at)` 为空会先在头一道门就返回 None，COUNT 那一句根本没跑到。
    """
    async def fake_change(self) -> float:
        return time.time()

    monkeypatch.setattr(StudioStore, "_last_change_ts", fake_change)

    assert await store.maybe_daily_snapshot() is None
    assert snap_files(store) == []
    assert await store.get_setting(SNAPSHOT_DAY_KEY) == "", "没取就不该记「今天取过了」"


# ==================== 保留策略与文件安全 ====================

async def test_prune_keeps_seven_and_ignores_strays(store, monkeypatch) -> None:
    """写 8 份只留 7，且 `.part` 与别人丢进来的文件不占名额。

    守住的是 `test_studio_backup.py` 里已经踩过的那个坑：半截文件 mtime 最新，
    算进「留最近 N 份」就会把一份真备份挤出去，而那一份恰恰是人最可能需要的。
    """
    await write_a_book(store)
    folder = store.backup_dir("snapshots")
    base = time.time() - 100          # 让「刚写的那一份」永远比被挪过 mtime 的新
    holder = {"n": 0}

    monkeypatch.setattr(StudioStore, "_stamp", lambda self: f"2026{holder['n']:04d}_000000")
    strays = [folder / "studio-daily-20260000_000000.sqlite.part", folder / "notes.txt"]
    strays[0].write_text("半截", encoding="utf-8")
    strays[1].write_text("顺手丢进来的", encoding="utf-8")

    made = []
    for i in range(8):
        holder["n"] = i
        made.append(await store.snapshot_now())
        os.utime(made[-1], (base + i, base + i))
        os.utime(strays[0], (base + 900, base + 900))
        os.utime(strays[1], (base + 800, base + 800))

    left = [p.name for p in snap_files(store)]
    assert len(left) == BACKUP_SNAPSHOT_KEEP, left
    assert made[0].name not in left, "最旧那一份该被清掉"
    assert made[-1].name in left, "最新那一份不该被半截文件挤出名额"
    assert strays[0].is_file() and strays[1].is_file(), "名单之外的文件轮不到清理来删"
    assert sorted(r["name"] for r in store.list_snapshot_backups()) == left, \
        "列举与清理必须认同一套名单"


async def test_snapshot_is_a_database_you_can_open(store) -> None:
    """快照得是真能用的库，而且快门按下那一刻稿子是完整的。

    只验「文件存在、体积不为 0」没有意义：半截文件也存在。真正要确认的是
    `integrity_check` 过、`works` 行数与主库一致、正文一字不差。
    """
    work = await write_a_book(store)
    before = len(await store.list_works())
    chapters = await store.list_readable(work["id"])
    bodies = [(await store.get_node(c["id"]))["content"] for c in chapters]

    dest = await store.snapshot_now()

    con = sqlite3.connect(dest)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM works").fetchone()[0] == before
        rows = con.execute(
            "SELECT title, content FROM nodes WHERE work_id = ? ORDER BY ordinal",
            (work["id"],)).fetchall()
    finally:
        con.close()
    assert [r[0] for r in rows] == [c["title"] for c in chapters]
    assert [r[1] for r in rows] == bodies, "稿子必须一字不差"

    # 取完之后又写了一本书：前一份里不该有它（快照是快门，不是软链接）
    await store.create_work(kind="novel", title="后来的一本书")
    con = sqlite3.connect(dest)
    try:
        assert con.execute("SELECT COUNT(*) FROM works").fetchone()[0] == before
    finally:
        con.close()


async def test_pruning_never_touches_the_live_database(store, monkeypatch) -> None:
    """清理只在 `backups/snapshots/` 里走，摸不到上一层的库本体。

    这一条要是写错，后果是「备份堆多了，顺手把稿子删了」——比没备份坏得多。
    快照目录与库文件只差一层目录名，靠后缀与目录名区分，必须真跑一遍。
    """
    await write_a_book(store)
    holder = {"n": 0}
    monkeypatch.setattr(StudioStore, "_stamp", lambda self: f"2026{holder['n']:04d}_000000")
    for i in range(BACKUP_SNAPSHOT_KEEP + 3):
        holder["n"] = i
        assert await store.snapshot_now() is not None

    assert store.db_path.is_file(), "当前库本体不能被清理掉"
    assert len(snap_files(store)) == BACKUP_SNAPSHOT_KEEP
    works = await store.list_works()
    assert len(works) == 1 and works[0]["title"] == "快照测试"
    assert len(await store.list_readable(works[0]["id"])) == 2, "书还在、字还在"
    # 删除副本那条线也不该被快照的保留策略波及：两个目录各数各的
    assert store.list_work_backups() == []


async def test_manual_snapshot_twice_in_one_second(store, monkeypatch) -> None:
    """同一秒连点两次「立即快照」不撞名。

    `VACUUM INTO` 遇同名文件直接报错，而时间戳只到秒。界面上这就是连点两下按钮，
    不能因为文件名撞了回一个谁也看不懂的 500。
    """
    await write_a_book(store)
    monkeypatch.setattr(StudioStore, "_stamp", lambda self: "20260101_000000")

    a = await store.snapshot_now()
    b = await store.snapshot_now()

    assert a.name == "studio-manual-20260101_000000.sqlite"
    assert b.name == "studio-manual-20260101_000000_1.sqlite"
    assert a.is_file() and b.is_file()
    # 手动这一份记「改动到哪一刻」，但不记「今天取过了」：
    # 早上手动一份不该把白天写完那份的退路顶掉
    assert await store.get_setting(SNAPSHOT_TS_KEY) != ""
    assert await store.get_setting(SNAPSHOT_DAY_KEY) == ""


async def test_unavailable_folder_does_not_raise(store, monkeypatch) -> None:
    """快照目录不可用时，后台那一条不抛异常，按钮那一条要能报出来。

    同一个失败在两条路径上的表现必须不同：循环是记账，写不成只留日志、下一轮再看；
    而人按下按钮时，「没写成」必须让他知道，不能显示成已经存好了。
    """
    await write_a_book(store)

    def boom(kind: str, action: str = "删除") -> None:
        raise BackupError(f"备份目录不可用，已拒绝{action}")

    monkeypatch.setattr(store, "_backup_dir", boom)

    assert await store.maybe_daily_snapshot() is None, "后台记账不能往上抛"
    assert await store.get_setting(SNAPSHOT_DAY_KEY) == "", "没写成就不记账，下一轮还会再试"
    with pytest.raises(BackupError):
        await store.snapshot_now()


# ==================== 接口层 ====================

def test_snapshot_routes_take_and_list(tmp_path) -> None:
    """`POST /studio/backups/snapshot` 与 `GET /studio/backups` 里的快照那一路。

    全程走 HTTP：aiosqlite 的连接绑在建立它的那个事件循环上。
    顺带钉住两条既有约定——界面上看不到本机路径，失败只回固定文案。
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
            "kind": "novel", "title": "快照接口", "scale": "single"}).json()
        client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "chapter", "title": "第一章", "content": BODY})

        assert client.get("/studio/backups").json()["snapshots"] == [], "没取过就该是空的"

        took = client.post("/studio/backups/snapshot")
        assert took.status_code == 200
        name = took.json()["name"]
        assert name.startswith("studio-manual-") and took.json()["bytes"] > 0

        rows = client.get("/studio/backups").json()
        assert [r["name"] for r in rows["snapshots"]] == [name]
        assert rows["snapshot_keep"] == BACKUP_SNAPSHOT_KEEP
        # 只给相对目录名：绝对路径会原样显示到界面上，把部署位置说出去
        for field in ("dir", "snapshot_dir"):
            assert not os.path.isabs(rows[field]) and str(tmp_path) not in rows[field], field

        # 目录写不成时：500 只回固定文案，异常原话里带着库的位置
        def boom():
            raise BackupError(f"备份目录不可用，已拒绝快照：{store.db_path}")

        store.snapshot_now = boom
        denied = client.post("/studio/backups/snapshot")
        assert denied.status_code == 500
        assert str(tmp_path) not in denied.json()["detail"], denied.json()["detail"]
