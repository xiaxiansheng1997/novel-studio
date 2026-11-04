"""模板体系：库、优先级链、内置只读与老库迁移。

盯的不是「表格能不能存」，而是这几件容易出错的事：

- 内置模板每次启动被 YAML 覆盖，所以任何直接改动都必须被拒绝，只能克隆出副本。
- `pick_template` 是一条六级优先链：同一类文章今天用 A 明天用 B，界面上看不出为什么，
  所以每一级的命中条件都要能复现。
- 删模板要先解绑作品：作品跟着消失是灾难，模板悄悄失效是骗人。
- 从作品沉淀骨架只用已经写出来的东西反推，不调模型；超 30 节要截断并说明。
- 老库补列：升级当天「公众号」作品要从文种迁到渠道，否则看起来像东西丢了。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio.store import StudioStore

OUTLINE = [
    {"heading": "开场", "prompt": "用一个具体场景抓人", "target_words": 400},
    {"heading": "主体", "prompt": "2-3 个做法，每个配例子", "target_words": 1200},
]


@pytest.fixture
async def store(tmp_path):
    s = StudioStore(tmp_path / "studio.db")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


def _app(tmp_path) -> StudioStore:
    """起一个只挂 studio 路由的 app：aiosqlite 的连接绑在建立它的那个事件循环上。"""
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
    _app.client_app = app  # 给调用方建 TestClient 用
    return store


# ==================== store 层：CRUD 与排序 ====================

async def test_create_update_delete_roundtrip(store) -> None:
    made = await store.create_template(
        kind="article", name="六段历史", subtype="history", channel="wechat",
        outline=OUTLINE, style_prompt="克制", checks=["citation_missing"],
        tags=["深度"], note="三千字以上",
    )
    assert made["id"].startswith("tpl_u_")
    assert made["is_builtin"] == 0
    assert made["channel"] == "wechat"
    assert (made["pinned"], made["is_default"]) == (0, 0)
    assert made["source_work_id"] == ""
    assert json.loads(made["outline_json"]) == OUTLINE
    assert json.loads(made["tags_json"]) == ["深度"]
    assert made["use_count"] == 0

    got = await store.update_template(
        made["id"], name="改名", channel="zhihu",
        outline=[{"heading": "只剩一节"}], tags=["a", "b"],
    )
    assert (got["name"], got["channel"]) == ("改名", "zhihu")
    assert json.loads(got["outline_json"]) == [{"heading": "只剩一节"}]
    assert json.loads(got["tags_json"]) == ["a", "b"]

    # 过滤按精确值：换到 zhihu 之后，按 zhihu 筛得到它，按 wechat 筛不到它
    assert [t["id"] for t in await store.list_templates(channel="zhihu")] == [made["id"]]
    assert made["id"] not in {t["id"] for t in await store.list_templates(channel="wechat")}

    assert await store.delete_template(made["id"]) == {"deleted": 1, "unbound": 0}
    assert await store.get_template(made["id"]) is None
    # 再删一次是 0，接口层靠这个值回 404，不该抛异常
    assert await store.delete_template(made["id"]) == {"deleted": 0, "unbound": 0}


async def test_update_unknown_template_returns_none(store) -> None:
    assert await store.update_template("tpl_u_nope", name="x") is None


async def test_pin_floats_to_the_top_and_toggles(store) -> None:
    plain = await store.create_template(kind="article", name="普通", outline=OUTLINE)
    starred = await store.create_template(kind="article", name="收藏的", outline=OUTLINE)

    assert (await store.toggle_template_pin(starred["id"]))["pinned"] == 1
    assert (await store.list_templates(kind="article"))[0]["id"] == starred["id"]
    # 不传 pinned 就是取反；显式传值就照办
    assert (await store.toggle_template_pin(starred["id"]))["pinned"] == 0
    assert (await store.toggle_template_pin(plain["id"], True))["pinned"] == 1


async def test_only_one_default_per_group(store) -> None:
    a = await store.create_template(kind="article", name="A", subtype="history",
                                    channel="wechat", outline=OUTLINE)
    b = await store.create_template(kind="article", name="B", subtype="history",
                                    channel="wechat", outline=OUTLINE)
    # 另一组（channel 为空）不该被这一组清掉
    other = await store.create_template(kind="article", name="C", subtype="history",
                                        outline=OUTLINE)

    assert await store.set_default_template(a["id"])
    assert (await store.get_template(a["id"]))["is_default"] == 1
    assert await store.set_default_template(b["id"])

    rows = await store.list_templates(kind="article", subtype="history", channel="wechat")
    assert [r["id"] for r in rows if r["is_default"]] == [b["id"]]

    assert await store.set_default_template(other["id"])
    assert (await store.get_template(b["id"]))["is_default"] == 1
    assert (await store.get_template(other["id"]))["is_default"] == 1


# ==================== pick_template 六级链 ====================

async def test_pick_template_walks_the_chain(store) -> None:
    # 4 级：YAML 里 subtype 声明的出厂默认（内置模板本身都不是 is_default）
    assert (await store.pick_template("article", "history", "wechat"))["id"] == "tpl_history"
    # 5 级：该 kind 下任意一个（novel 没有 subtype 指定默认）
    assert (await store.pick_template("novel", "", ""))["id"] == "tpl_novel"

    # 1 级：kind+subtype+channel 的默认
    exact = await store.create_template(kind="article", name="公众号历史",
                                        subtype="history", channel="wechat", outline=OUTLINE)
    await store.set_default_template(exact["id"])
    assert (await store.pick_template("article", "history", "wechat"))["id"] == exact["id"]

    # 2 级：同 subtype 的默认（渠道对不上时降一级）
    by_sub = await store.create_template(kind="article", name="通用历史",
                                         subtype="history", outline=OUTLINE)
    await store.set_default_template(by_sub["id"])
    assert (await store.pick_template("article", "history", "zhihu"))["id"] == by_sub["id"]

    # 3 级：只按渠道（该 subtype 还没有默认）
    by_ch = await store.create_template(kind="article", name="任何文体都能用的知乎稿",
                                        channel="zhihu", outline=OUTLINE)
    await store.set_default_template(by_ch["id"])
    assert (await store.pick_template("article", "tech", "zhihu"))["id"] == by_ch["id"]

    # 同 subtype 有默认时，subtype 优先于渠道
    tech = await store.create_template(kind="article", name="技术默认",
                                       subtype="tech", outline=OUTLINE)
    await store.set_default_template(tech["id"])
    assert (await store.pick_template("article", "tech", "zhihu"))["id"] == tech["id"]


async def test_pick_template_returns_none_when_library_is_empty(store) -> None:
    """6 级：库空了就是没有骨架，S0 退化为不出内容。"""
    for tpl in await store.list_templates():
        await store.delete_template(tpl["id"])
    assert await store.pick_template("article", "history", "wechat") is None


# ==================== 接口层 ====================

def test_builtin_is_readonly_and_duplicate_is_the_only_way_in(tmp_path) -> None:
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        builtin = client.get("/studio/templates/tpl_history").json()
        assert builtin["is_builtin"] == 1

        bad = client.patch("/studio/templates/tpl_history", json={"name": "偷偷改内置"})
        assert bad.status_code == 409
        # 只说 409 用户不知道该怎么做，detail 要能指路
        assert "复制" in bad.json()["detail"]
        assert client.delete("/studio/templates/tpl_history").status_code == 409
        assert client.get("/studio/templates/tpl_history").json()["name"] == builtin["name"]

        copy = client.post("/studio/templates/tpl_history/duplicate").json()
        assert copy["is_builtin"] == 0 and copy["id"].startswith("tpl_u_")
        assert copy["name"].endswith("（副本）")
        assert (copy["subtype"], copy["channel"]) == ("history", "")
        assert json.loads(copy["outline_json"]) == json.loads(builtin["outline_json"])

        renamed = client.patch(f"/studio/templates/{copy['id']}", json={
            "name": "我的历史骨架", "outline": [{"heading": "只剩一节", "prompt": "x"}]})
        assert renamed.status_code == 200
        assert json.loads(renamed.json()["outline_json"])[0]["heading"] == "只剩一节"
        # 副本改了，原内置一个字都没动
        again = client.get("/studio/templates/tpl_history").json()
        assert again["name"] == builtin["name"]
        assert again["outline_json"] == builtin["outline_json"]

        # 空骨架与空名字都进不来：没有 heading 的节进了 S0，模型会写一段没主题的话
        assert client.patch(f"/studio/templates/{copy['id']}",
                            json={"outline": []}).status_code == 400
        assert client.patch(f"/studio/templates/{copy['id']}",
                            json={"name": "   "}).status_code == 400
        assert client.get("/studio/templates/tpl_u_不存在").status_code == 404


def test_list_filters_use_count_and_search(tmp_path) -> None:
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        tpl = client.post("/studio/templates", json={
            "kind": "article", "name": "知乎历史骨架", "subtype": "history",
            "channel": "zhihu", "outline": [{"heading": "开场"}], "note": "结论先行",
        }).json()
        client.post("/studio/works", json={
            "kind": "article", "title": "用一次", "template_id": tpl["id"]})

        rows = client.get("/studio/templates", params={"channel": "zhihu"}).json()
        assert [r["id"] for r in rows] == [tpl["id"]]
        assert rows[0]["use_count"] == 1
        assert (rows[0]["is_builtin"], rows[0]["is_default"]) == (0, 0)

        assert [r["id"] for r in client.get("/studio/templates", params={"q": "知乎"}).json()] \
            == [tpl["id"]]
        assert client.get("/studio/templates", params={"q": "不存在的名字"}).json() == []


def test_delete_unbinds_the_works_using_it(tmp_path) -> None:
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        tpl = client.post("/studio/templates", json={
            "kind": "article", "name": "临时骨架", "outline": [{"heading": "开场"}]}).json()
        work = client.post("/studio/works", json={
            "kind": "article", "title": "用它写的一篇", "template_id": tpl["id"]}).json()
        assert work["template_id"] == tpl["id"]

        got = client.delete(f"/studio/templates/{tpl['id']}").json()
        assert got == {"deleted": 1, "unbound": 1}
        # 作品还在，只是解绑：删模板不该顺手删掉人的稿子
        assert client.get(f"/studio/works/{work['id']}").status_code == 200
        assert client.get(f"/studio/works/{work['id']}").json()["template_id"] is None


def test_create_work_picks_by_default_but_honours_explicit_none(tmp_path) -> None:
    """`template_id` 三态：缺省 = 帮我挑，空串 = 不套模板，具体 id = 用户选的。"""
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        auto = client.post("/studio/works", json={
            "kind": "article", "subtype": "history", "channel": "wechat", "title": "自动挑"}).json()
        assert auto["template_id"] == "tpl_history"  # YAML 的出厂默认
        assert auto["channel"] == "wechat"

        bare = client.post("/studio/works", json={
            "kind": "article", "title": "不套模板", "template_id": ""}).json()
        # 空串必须留住：当没填的话，界面上「不套模板」那个选项会静默失效。
        # 库里落成 NULL——那一列是指向 templates 的外键，空串会直接撞上。
        assert bare["template_id"] is None

        # 反过来也得能清：写作台的「不套模板」走 PATCH 写空串
        assert client.patch(f"/studio/works/{auto['id']}",
                            json={"template_id": ""}).json()["template_id"] is None


def test_from_work_sediments_the_outline(tmp_path) -> None:
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        work = client.post("/studio/works", json={
            "kind": "article", "scale": "single", "subtype": "history",
            "channel": "wechat", "title": "一段被忽略的历史", "template_id": ""}).json()
        made = []
        for title, body in [
            ("开场", "雍正三年的冬天格外冷，山西的雪压塌了半座县城，报灾的折子走了半个月。"),
            ("经过", "朝廷的赈灾粮在两个月后才到，中间那段时间发生了什么，正史里没有记载。"),
            ("余波", "此后十年，这一带再也没有出现过同规模的灾荒记录，县志里只写了一行。"),
        ]:
            made.append(client.post(f"/studio/works/{work['id']}/nodes", json={
                "kind": "section", "title": title, "content": body}).json())
        # 第二节有节拍卡：它比正文前 120 字更能说明「这一节要写什么」
        client.patch(f"/studio/nodes/{made[1]['id']}", json={"summary": "赈灾粮的迟到与地方自救"})
        # 文风也要跟着沉：沉淀出的模板将来要用同一套语气开新稿
        client.patch(f"/studio/works/{work['id']}", json={
            "style_json": json.dumps({"tone": "克制的史笔，不煽情"}, ensure_ascii=False)})
        # 第四节一个字都没写：target_words 要兜底 800，不能是 0
        made.append(client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "section", "title": "还没写的", "content": ""}).json())

        tpl = client.post(f"/studio/templates/from-work/{work['id']}", json={}).json()

        assert (tpl["kind"], tpl["subtype"], tpl["channel"]) == ("article", "history", "wechat")
        assert tpl["source_work_id"] == work["id"]
        assert tpl["is_builtin"] == 0
        assert tpl["name"] == "一段被忽略的历史 · 骨架"
        assert tpl["truncated"] == 0

        outline = json.loads(tpl["outline_json"])
        assert [s["heading"] for s in outline] == ["开场", "经过", "余波", "还没写的"]
        assert outline[1]["prompt"] == "赈灾粮的迟到与地方自救"
        assert outline[0]["prompt"].startswith("雍正三年的冬天")
        assert len(outline[0]["prompt"]) <= 120
        assert outline[0]["target_words"] == made[0]["word_count"] > 0
        assert outline[3]["target_words"] == 800
        # 检查项来自作品自己的 resolve（历史 + 公众号），不是模板自带的
        checks = json.loads(tpl["checks_json"])
        assert {"citation_missing", "absolute_claim"} <= set(checks)
        # 文风取自该作品的样式表，与装配时同一份口径（_style_text）
        assert "克制的史笔" in tpl["style_prompt"]

        # 名字可以覆盖
        named = client.post(f"/studio/templates/from-work/{work['id']}",
                            json={"name": "我的四段式"}).json()
        assert named["name"] == "我的四段式"


def test_from_work_truncates_at_thirty_sections(tmp_path) -> None:
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        work = client.post("/studio/works", json={
            "kind": "article", "title": "三十二节", "template_id": ""}).json()
        for i in range(32):
            client.post(f"/studio/works/{work['id']}/nodes", json={
                "kind": "section", "title": f"第{i + 1}节", "content": "正文"})

        tpl = client.post(f"/studio/templates/from-work/{work['id']}", json={}).json()

        outline = json.loads(tpl["outline_json"])
        assert len(outline) == 30
        assert tpl["truncated"] == 2
        assert outline[0]["heading"] == "第1节" and outline[-1]["heading"] == "第30节"


def test_export_import_roundtrip(tmp_path) -> None:
    _app(tmp_path)
    with TestClient(_app.client_app) as client:
        dump = client.get("/studio/templates/tpl_history/export").json()
        assert dump["format"] == "novel-studio.template/1"
        assert dump["template"]["name"] == "历史专题"
        # 绑本机库的字段不该带出去：换台机器 id 一定不同
        assert "id" not in dump["template"]
        assert "is_builtin" not in dump["template"]
        assert "use_count" not in dump["template"]

        made = client.post("/studio/templates/import", json=dump).json()
        assert made["is_builtin"] == 0
        assert made["id"] != "tpl_history"  # 导入即新建，不覆盖同 id
        assert made["subtype"] == "history"
        assert json.loads(made["outline_json"]) == dump["template"]["outline"]

        assert client.post("/studio/templates/import", json={"name": ""}).status_code == 400
        # 没有骨架就没有可套的东西
        assert client.post("/studio/templates/import",
                           json={"name": "空骨架", "outline": []}).status_code == 400


# ==================== 老库迁移 ====================

async def test_migrate_adds_channel_columns_and_backfills(tmp_path) -> None:
    """老库没有 channel 列：init() 之后要补上，并把「文种=公众号」转成「渠道=公众号」。

    不迁的话，升级当天历史公众号作品会突然变成「没有渠道」，界面上的
    「公众号」那一档也随之消失——用户只会觉得东西丢了。
    """
    db = tmp_path / "legacy.db"
    conn = await aiosqlite.connect(str(db))
    await conn.executescript(
        """
        CREATE TABLE works (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, scale TEXT DEFAULT 'single',
            subtype TEXT DEFAULT '', title TEXT NOT NULL, premise TEXT DEFAULT '',
            status TEXT DEFAULT 'active', template_id TEXT, target_words INTEGER DEFAULT 0,
            style_json TEXT DEFAULT '{}', meta_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE templates (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, subtype TEXT DEFAULT '',
            name TEXT NOT NULL, outline_json TEXT NOT NULL, style_prompt TEXT DEFAULT '',
            checks_json TEXT DEFAULT '[]', is_builtin INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        );
        """
    )
    await conn.execute(
        "INSERT INTO works(id, kind, subtype, title, created_at, updated_at) "
        "VALUES('w_old', 'article', 'wechat', '老公众号稿', 1.0, 1.0)")
    await conn.execute(
        "INSERT INTO templates(id, kind, subtype, name, outline_json, is_builtin, created_at) "
        "VALUES('tpl_old', 'article', 'wechat', '老的公众号模板', '[]', 1, 1.0)")
    await conn.commit()
    await conn.close()

    s = StudioStore(db)
    await s.init()
    try:
        work = await s.get_work("w_old")
        assert (work["channel"], work["subtype"]) == ("wechat", "general")

        tpl = await s.get_template("tpl_old")
        assert (tpl["channel"], tpl["subtype"]) == ("wechat", "general")
        # 新列的默认值也得是可读的，否则界面上是一排 None
        assert tpl["tags_json"] == "[]"
        assert (tpl["pinned"], tpl["is_default"]) == (0, 0)
        assert tpl["source_work_id"] == ""
        # 老库照样能被 YAML 播种：新内置模板不因为库老就不出现
        assert (await s.get_template("tpl_history"))["is_builtin"] == 1
    finally:
        await s.close()
