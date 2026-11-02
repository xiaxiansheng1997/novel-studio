"""全作品查找替换：替换前先把旧文存进版本表。

这一条动的是用户的稿子，而且一次摊到几十上百节，所以测试盯的不是「能不能改」，
而是**改错了我能不能回去**、以及**它有没有顺手谎报别的东西**：

- 旧文必须能在版本表里找回来，且历史里不出现两行一模一样的正文（判据同回滚）。
- 日历上的「今天写了多少字」不能被批量替换刷高——与「导入旧稿不算今天写的字」同口径。
- 默认 dry_run：一次调用都不该发出去，`index_state` 那行连 `updated_at` 都不许变。
- 一个术语重名不该毁掉整批替换。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import replace as repl
from app.studio.store import StudioStore, _count_words

CH1 = "林知微推门进去，雪落了满山。她把信放在桌上，转身离开。"
CH2 = "林知微第二次来到这里，灯亮到天明。她什么也没有说。"
CH3 = "山门外的人等了整夜，终究没有等到回信。"
FIND = "林知微"
REPL = "沈砚青"


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def book(store, title: str = "替换测试") -> tuple[dict, list[dict]]:
    """一本三章的书：前两章各有两处人名，第三章不含命中。"""
    work = await store.create_work(kind="novel", title=title)
    nodes = []
    for i, body in enumerate((CH1, CH2, CH3)):
        nodes.append(await store.create_node(work_id=work["id"], kind="chapter",
                                             title=f"第{i + 1}章", content=body, ordinal=i + 1))
    return work, nodes


async def do(store, work_id: str, **kw):
    args = {"find": FIND, "replace": REPL, "dry_run": False}
    args.update(kw)
    return await repl.replace_in_work(store, work_id=work_id, **args)


async def contents(store, ids: list[str]) -> list[str]:
    return [(await store.get_node(nid))["content"] for nid in ids]


async def version_texts(store, node_id: str) -> list[str]:
    """历史列表按 version_no 倒序返回，正文要按 id 单独取——能回滚的就是这些。"""
    out = []
    for v in await store.list_versions(node_id):
        full = await store.get_version(v["id"])
        out.append((full or {}).get("content") or "")
    return out


# ==================== 纯函数：命中与切片 ====================

def test_find_all_and_apply_replace_on_plain_text() -> None:
    text = "甲甲甲，甲。"
    assert repl.find_all(text, "甲") == [(0, 1), (1, 2), (2, 3), (4, 5)]
    assert repl.apply_replace(text, "甲", "乙")[0] == "乙乙乙，乙。"
    # 空串不算命中：一条「查找内容为空」的替换不该把正文切成一堆碎片
    assert repl.find_all(text, "") == []
    assert repl.apply_replace(text, "", "乙") == (text, 0)


def test_case_insensitive_keeps_the_original_casing() -> None:
    text = "Anna met ANNA and wrote anna down, KEEP ME."
    assert repl.find_all(text, "anna", case_sensitive=True) == [(24, 28)]
    hits = repl.find_all(text, "anna", case_sensitive=False)
    assert [text[lo:hi] for lo, hi in hits] == ["Anna", "ANNA", "anna"]
    # 大小写不敏感只用来定位，切片一律用原串：命中之外的正文不该被整体降格成小写
    new, n = repl.apply_replace(text, "anna", "Bella", case_sensitive=False)
    assert n == 3
    assert new == "Bella met Bella and wrote Bella down, KEEP ME."


# ==================== dry_run ====================

async def test_dry_run_writes_not_a_single_byte(store) -> None:
    """预览一次：正文逐字不变、一次 commit 都不发、索引台账连时间戳都不许动。"""
    work, nodes = await book(store)
    ids = [n["id"] for n in nodes]
    before_text = await contents(store, ids)
    before_state = await store.index_state(work["id"])

    r = await repl.replace_in_work(store, work_id=work["id"], find=FIND, replace=REPL)

    assert r["dry_run"] is True
    assert r["total_hits"] == 2 and r["total_nodes"] == 2
    assert r["scanned_nodes"] == 3
    assert [m["ordinal"] for m in r["matches"]] == [1, 2]
    assert r["matches"][0]["chars_before"] == len(CH1)
    assert r["matches"][0]["chars_after"] == len(CH1) - len(FIND) * 2 + len(REPL) * 2
    assert "new_content" not in r["matches"][0], "预览不该把整段新正文塞进返回体"
    assert await contents(store, ids) == before_text
    assert await store.index_state(work["id"]) == before_state
    assert (await store.list_versions(ids[0])) == []


# ==================== 旧文进版本表 / 不刷日历 ====================

async def test_old_text_is_recoverable_and_history_has_no_duplicates(store) -> None:
    """替换前的那一版必须找得回来，而且历史里不许出现两行相同的正文。"""
    work, nodes = await book(store)
    n1 = nodes[0]["id"]

    r = await do(store, work["id"])

    assert r["replaced_nodes"] == 2 and r["replaced_hits"] == 2
    assert (await store.get_node(n1))["content"] == CH1.replace(FIND, REPL)
    # 倒序的第一行是「改后」那份（update_node 的快照存的是改后正文），
    # 第二行才是替换前抢下来的旧稿——这一行存在的意义就是「改错了还能回去」
    assert [v["source"] for v in await store.list_versions(n1)] == ["bulk_replace", "human"]
    texts = await version_texts(store, n1)
    assert CH1 in texts, "替换前的稿子必须在版本表里找得回来"
    assert len(set(texts)) == len(texts), f"历史里出现了重复行：{texts}"
    assert r["summaries_stale"] == 2

    # 紧接着再替换另一处：当前正文已经进过版本表，不该重复补一行
    again = await do(store, work["id"], find="转身离开", replace="推门出去")
    assert again["replaced_nodes"] == 1
    texts2 = await version_texts(store, n1)
    assert len(texts2) == 3, f"第二次替换多补了一行重复历史：{texts2}"
    assert len(set(texts2)) == len(texts2), f"历史里出现了重复行：{texts2}"


async def test_bulk_replace_does_not_feed_the_calendar(store) -> None:
    """批量替换不许刷高「今天写的字数」：那几行字是人昨天写的，只是换了个名字。"""
    work, _ = await book(store)
    assert await do(store, work["id"])

    rows = await store.daily_writes(days=30)
    assert sum(int(r["chars"]) for r in rows) == 0, f"日历被刷高了：{rows}"
    # 同一节里真手改一次，字数就该记上——不是所有写入都被吞掉
    n1 = (await store.list_readable(work["id"]))[0]["id"]
    assert await store.update_node(n1, content="手写的一段新正文，加了三百字。" * 3)
    assert sum(int(r["chars"]) for r in await store.daily_writes(days=30)) > 0


async def test_zero_hits_creates_no_version_rows(store) -> None:
    work, nodes = await book(store)

    dry = await repl.replace_in_work(store, work_id=work["id"], find="没有这个名字")
    assert dry["total_hits"] == 0 and dry["matches"] == [] and dry["warnings"] == []

    real = await do(store, work["id"], find="没有这个名字")
    assert real["replaced_nodes"] == 0 and real["replaced_hits"] == 0
    assert real["index"]["skipped"] is True, "没改东西就不该重跑一遍全书索引"
    for n in nodes:
        assert await store.list_versions(n["id"]) == []
    assert (await store.get_node(nodes[0]["id"]))["content"] == CH1


# ==================== 范围 / 术语 ====================

async def test_scope_node_touches_only_that_node(store) -> None:
    work, nodes = await book(store)
    n2 = nodes[1]["id"]

    r = await do(store, work["id"], scope="node", node_id=n2)

    assert r["dry_run"] is False and r["scope"] == "node"
    assert r["replaced_nodes"] == 1 and r["replaced_hits"] == 1
    assert (await store.get_node(n2))["content"] == CH2.replace(FIND, REPL)
    # 同一本书里另一节同样的串，一个字都没动，连历史都不该多一行
    assert (await store.get_node(nodes[0]["id"]))["content"] == CH1
    assert await store.list_versions(nodes[0]["id"]) == [], "没动的那一节不该有版本行"
    # 别的作品的节 id 不该被这一本用到
    other = await store.create_work(kind="novel", title="另一本书")
    alien = await store.create_node(work_id=other["id"], kind="chapter", title="外部的一节",
                                    content=FIND * 2, ordinal=1)
    none = await do(store, work["id"], scope="node", node_id=alien["id"])
    assert none["replaced_nodes"] == 0
    assert (await store.get_node(alien["id"]))["content"] == FIND * 2


async def test_terms_follow_the_rename_but_a_clash_does_not_kill_the_batch(store) -> None:
    """术语表里同名的那条跟着改；改成已有术语名时只跳过那一条，整批替换照样成功。"""
    work, nodes = await book(store)
    await store.upsert_term(work["id"], term=FIND, definition="主角")
    n1, n2 = nodes[0]["id"], nodes[1]["id"]

    dry = await repl.replace_in_work(store, work_id=work["id"], find=FIND, replace=REPL)
    assert dry["term_hits"] == 1, "术语表里同名的那一条得在预览里就报出来"

    r = await do(store, work["id"])

    assert [t["from"] for t in r["terms_updated"]] == [FIND]
    assert [t["to"] for t in r["terms_updated"]] == [REPL]
    rows = await store.list_terms(work["id"])
    assert [(t["term"], t["definition"]) for t in rows] == [(REPL, "主角")], \
        f"术语要么没跟着改名，要么释义被顺手改掉了：{rows}"
    assert (await store.get_node(n1))["content"] == CH1.replace(FIND, REPL)
    assert (await store.get_node(n2))["content"] == CH2.replace(FIND, REPL)

    # 真的重名场景：新名字与这本书里已有的术语撞车，那一条跳过、整批照样成功
    work2, nodes2 = await book(store, title="第二本")
    await store.upsert_term(work2["id"], term=FIND, definition="旧名")
    await store.upsert_term(work2["id"], term=REPL, definition="撞名的那一条")
    r2 = await do(store, work2["id"])

    assert r2["replaced_nodes"] == 2, "一个术语的重名不该让整批替换失败"
    assert r2["terms_updated"] == []
    assert any("已经有同名术语了" in w for w in r2["warnings"]), r2["warnings"]
    assert (await store.get_node(nodes2[0]["id"]))["content"] == CH1.replace(FIND, REPL)
    assert (await store.get_node(nodes2[1]["id"]))["content"] == CH2.replace(FIND, REPL)
    # 撞名那一条原样留着，没被清空也没被改成半截
    assert {t["term"] for t in await store.list_terms(work2["id"])} == {FIND, REPL}


async def test_empty_replacement_keeps_the_term_row(store) -> None:
    """删掉一个说法（替换成空串）时，术语名不能被改成空字符串。"""
    work, _ = await book(store)
    await store.upsert_term(work["id"], term=FIND, definition="主角")

    dry = await repl.replace_in_work(store, work_id=work["id"], find=FIND, replace="")
    assert any("术语名不能空" in w for w in dry["warnings"]), "预览就该说出术语不会跟着改"

    r = await do(store, work["id"], replace="")

    assert r["replaced_nodes"] == 2
    assert r["terms_updated"] == [], "术语名不能被改成空字符串"
    assert any("术语名不能空" in w for w in r["warnings"]), r["warnings"]
    assert {t["term"] for t in await store.list_terms(work["id"])} == {FIND}


# ==================== 接口层 ====================

def _app(tmp_path) -> StudioStore:
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
    _app.client_app = app      # 给调用方建 TestClient 用
    return store


def test_replace_route_search_and_validation(tmp_path) -> None:
    """`POST /studio/works/{id}/replace` 全流程，外加三条校验与「搜索跟着替换走」。

    全程走 HTTP：aiosqlite 的连接绑在建立它的那个事件循环上。
    """
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        work = client.post("/studio/works", json={
            "kind": "novel", "title": "替换接口", "scale": "series"}).json()
        made = [client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "chapter", "title": f"第{i + 1}章", "content": body}).json()
            for i, body in enumerate((CH1, CH2, CH3))]
        n1 = made[0]["id"]
        client.post(f"/studio/works/{work['id']}/terms", json={"term": FIND, "definition": "主角"})

        # 校验：空串与全是空白都回 400（不是 422 那种字段报错），scope=node 少了 id 也回 400
        assert client.post(f"/studio/works/{work['id']}/replace", json={"find": ""}).status_code == 400
        assert client.post(f"/studio/works/{work['id']}/replace",
                           json={"find": "   "}).status_code == 400
        assert client.post(f"/studio/works/{work['id']}/replace",
                           json={"find": FIND, "scope": "node"}).status_code == 400
        assert client.post("/studio/works/w_不存在/replace",
                           json={"find": FIND}).status_code == 404
        # 长度上限：300 个字的查找串进不来
        assert client.post(f"/studio/works/{work['id']}/replace",
                           json={"find": "林" * 201}).status_code == 422

        # 默认就是 dry_run：不带 dry_run 的一次调用不能改任何东西
        peek = client.post(f"/studio/works/{work['id']}/replace",
                           json={"find": FIND, "replace": REPL}).json()
        assert peek["dry_run"] is True and peek["total_hits"] == 2
        before = client.get(f"/studio/nodes/{n1}").json()
        assert before["content"] == CH1

        took = client.post(f"/studio/works/{work['id']}/replace", json={
            "find": FIND, "replace": REPL, "dry_run": False}).json()
        assert took["dry_run"] is False and took["replaced_nodes"] == 2
        assert took["terms_updated"][0]["to"] == REPL
        assert took["index"]["nodes"] == 3
        assert any("图谱" in w for w in took["warnings"]), took["warnings"]
        assert any("待重算" in w for w in took["warnings"]), took["warnings"]

        # 人名等长，这一趟字数不该变；哈希必须跟着正文走（右栏与「历史」页都靠这两列）
        node = client.get(f"/studio/nodes/{n1}").json()
        assert node["content"] == CH1.replace(FIND, REPL)
        assert node["word_count"] == before["word_count"] == _count_words(node["content"])
        assert node["content_hash"] != before["content_hash"]

        # 「历史」页：倒序第一行是改后的那份，第二行才是替换前抢下来的旧稿
        versions = client.get(f"/studio/nodes/{n1}/versions").json()
        assert [v["source"] for v in versions] == ["bulk_replace", "human"], versions

        # 再替换一个长短语：改了字数，这两列就得跟着改
        shrink = client.post(f"/studio/works/{work['id']}/replace", json={
            "find": "雪落了满山", "replace": "雪很大", "dry_run": False}).json()
        assert shrink["replaced_nodes"] == 1
        shorter = client.get(f"/studio/nodes/{n1}").json()
        assert shorter["word_count"] == node["word_count"] - 2, shorter
        assert shorter["content_hash"] != node["content_hash"]

        # 检索：新词搜得到、旧词搜不到。索引是替换结束后增量重建的，不靠人再点一次重建
        new_hits = client.get("/studio/search", params={"q": REPL}).json()
        assert new_hits["count"] >= 1, new_hits
        old_hits = client.get("/studio/search", params={"q": FIND}).json()
        assert old_hits["count"] == 0, old_hits

        # 批量替换那一档被接口认下；不在名单里的来源仍然回 400
        assert client.patch(f"/studio/nodes/{n1}", json={
            "summary": "改一句摘要", "snapshot_source": "bulk_replace"}).status_code == 200
        bad = client.patch(f"/studio/nodes/{n1}", json={"summary": "x", "snapshot_source": "hacker"})
        assert bad.status_code == 400
        assert "bulk_replace" in bad.json()["detail"]


async def test_case_insensitive_store_path(store) -> None:
    """区分大小写关掉之后，英文人名的大小写变体一起改，但正文其余部分不降格。"""
    work = await store.create_work(kind="novel", title="英文名")
    n1 = await store.create_node(work_id=work["id"], kind="chapter", title="第一章",
                                 content="Anna said: ANNA is fine.\n", ordinal=1)
    n2 = await store.create_node(work_id=work["id"], kind="chapter", title="第二章",
                                 content="anna left early.\n", ordinal=2)

    dry = await repl.replace_in_work(store, work_id=work["id"], find="anna", replace="Bella",
                                     case_sensitive=False, dry_run=True)
    assert dry["total_hits"] == 3

    real = await repl.replace_in_work(store, work_id=work["id"], find="anna", replace="Bella",
                                      case_sensitive=False, dry_run=False)
    assert real["replaced_nodes"] == 2
    assert (await store.get_node(n1["id"]))["content"] == "Bella said: Bella is fine.\n"
    assert (await store.get_node(n2["id"]))["content"] == "Bella left early.\n"

    # 反过来：区分大小写时只动那一处，其余保持原样
    once = await repl.replace_in_work(store, work_id=work["id"], find="Bella", replace="Clara",
                                      case_sensitive=True, dry_run=False)
    assert once["replaced_hits"] == 3
