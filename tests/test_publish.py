"""公众号链路：富文本渲染、发布前检查、摘要。

公众号编辑器不认 Markdown（粘 `.md` 进去是一堆井号），也不吃 `<style>`/class，
所以渲染必须全内联；发布前的坑（段落过长、中文里混半角标点、摘要没填、风险词）
全是本地判得出的，不该占模型调用。这一组用例把这三件事钉住。
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import publish, wechat
from app.studio.store import StudioStore

LONG_PARA = "这一段没有任何分段，从头到尾都在讲同一件事，读起来会很累。" * 6
BODY = (
    "开头先给一个具体场景，让读者知道这篇跟他有关。\n\n"
    "**判断**：这件事的代价不在工具，而在流程。\n\n"
    "- 第一件可执行的事\n- 第二件可执行的事\n\n"
    "> 引用一句能记住的话。\n\n"
    "[图：一张打开率随时间下滑的折线图]\n\n"
    "最后问一句：你现在是怎么做的？"
)


class FakeGateway:
    def __init__(self, reply: str = "一段不超过一百二十字的摘要。") -> None:
        self.reply = reply
        self.completed: list[dict] = []

    def resolve(self, model: str):
        return ("fake", "")

    async def stream(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
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


def make_article(client: TestClient, *, sections: int = 2) -> dict:
    work = client.post("/studio/works", json={
        "kind": "article", "scale": "single", "subtype": "wechat",
        "title": "公众号测试", "premise": "讲清公众号的读法",
    }).json()
    for i in range(sections):
        client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "section", "title": f"第 {i + 1} 节", "content": BODY,
        })
    return work


# ==================== 渲染 ====================


def test_html_keeps_everything_inline_and_escapes_text() -> None:
    """公众号会剥掉 style 标签与 class，只留内联；正文里的尖括号必须转义。"""
    page = wechat.render_article([{
        "title": "第一节",
        "content": "正文<script>alert(1)</script>与 **重点**。",
    }])

    assert 'style="' in page
    assert "<style" not in page and "class=" not in page
    assert "&lt;script&gt;" in page, "正文里的标签必须转义，不能带进 HTML"
    assert "<strong>重点</strong>" in page
    assert "<h2" in page and "第一节" in page


def test_html_covers_lists_quotes_code_and_images() -> None:
    content = (
        "- 第一件\n- 第二件\n\n"
        "> 引用一句\n\n"
        "```python\nprint(1)\n```\n\n"
        "[图：一张曲线图]\n\n"
        "普通段落。"
    )
    page = wechat.render_article([{"title": "节", "content": content}])

    assert "<ul" in page and "<li" in page
    assert "<blockquote" in page
    assert "<pre" in page and "print(1)" in page
    assert "配图：一张曲线图" in page
    assert "<p" in page and "普通段落。" in page


def test_code_fence_blank_lines_do_not_split_blocks() -> None:
    content = "```\na\n\nb\n```"
    blocks = wechat.split_blocks(content)
    assert len(blocks) == 1, blocks


def test_title_is_opt_in() -> None:
    """公众号标题在后台单独填，正文里默认不再重复一遍。"""
    plain = wechat.render_article([{"title": "节", "content": "正文"}], title="作品名")
    assert "作品名" not in plain
    with_title = wechat.render_article([{"title": "节", "content": "正文"}],
                                       title="作品名", include_title=True)
    assert "作品名" in with_title


def test_images_and_stats() -> None:
    sections = [
        {"id": "n1", "title": "一", "content": "[图：封面]\n\n正文正文"},
        {"id": "n2", "title": "二", "content": "更多正文"},
    ]
    images = wechat.extract_images(sections)
    assert images == [{"node_id": "n1", "section": "一", "desc": "封面"}]
    got = wechat.stats(sections)
    assert got["sections"] == 2
    assert got["chars"] == len("正文正文更多正文")
    assert got["read_minutes"] == 1


# ==================== 发布前检查 ====================


def test_check_reports_the_things_that_break_a_wechat_post() -> None:
    work = {"title": "这个标题实在是太长了长到列表页根本显示不下" * 2}
    sections = [{
        "id": "n1",
        "title": "第一节",
        "content": LONG_PARA + "\n\n这是最好的办法,真的。",
    }]

    items = publish.check(work, sections, digest="")
    rules = {i["rule"] for i in items}

    assert "digest_missing" in rules
    assert "title_long" in rules
    assert "too_few_sections" in rules
    assert "long_paragraph" in rules
    assert "halfwidth_punct" in rules
    assert "risk_word" in rules
    # 风险词条目得说清是哪个词，不能只报一句「有风险词」
    hit = next(i for i in items if i["rule"] == "risk_word")
    assert "最好" in hit["message"] and hit["evidence"]


def test_check_is_quiet_on_a_clean_post() -> None:
    work = {"title": "公众号怎么写才有人读完"}
    sections = [
        {"id": "n1", "title": "开头", "content": "先说一个具体场景。\n\n第二段。"},
        {"id": "n2", "title": "收尾", "content": "你会怎么选？留言聊聊。"},
    ]
    items = publish.check(work, sections, digest="一段合规的摘要，讲清这篇在说什么。")
    rules = {i["rule"] for i in items}
    assert "long_paragraph" not in rules
    assert "risk_word" not in rules
    assert "no_cta" not in rules
    assert "digest_missing" not in rules


def test_risk_words_list_is_not_empty() -> None:
    """词表写坏了（YAML 语法错）时应该静默退回空表，而不是让接口报错。"""
    assert publish.risk_hits("这是一段正常的话") == []
    assert any(h["word"] == "稳赚不赔" for h in publish.risk_hits("号称稳赚不赔"))


# ==================== 接口 ====================


def test_export_html_endpoint(tmp_path) -> None:
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        got = client.get(f"/studio/works/{work['id']}/export?format=html")
        assert got.status_code == 200, got.text
        assert got.headers["content-type"].startswith("text/html")
        assert "第 1 节" in got.text and "<p" in got.text
        assert "公众号测试" not in got.text, "标题默认不重复进正文"
        # md 那条路还在
        md = client.get(f"/studio/works/{work['id']}/export")
        assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")


def test_publish_check_endpoint(tmp_path) -> None:
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        got = client.get(f"/studio/works/{work['id']}/publish-check")
        assert got.status_code == 200, got.text
        data = got.json()
        assert data["stats"]["sections"] == 2
        assert data["stats"]["chars"] > 0
        assert any(i["rule"] == "digest_missing" for i in data["items"])
        assert data["images"][0]["desc"].startswith("一张打开率")


def test_digest_is_written_to_the_work(tmp_path) -> None:
    gw = FakeGateway("工具不是问题，流程才是。")
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        got = client.post(f"/studio/works/{work['id']}/digest", json={})
        assert got.status_code == 200, got.text
        assert got.json()["digest"] == "工具不是问题，流程才是。"
        assert client.get(f"/studio/works/{work['id']}").json()["digest"] == "工具不是问题，流程才是。"
        # 摘要写完之后，发布检查里不该再说「没填摘要」
        items = client.get(f"/studio/works/{work['id']}/publish-check").json()["items"]
        assert not any(i["rule"] == "digest_missing" for i in items)


def test_digest_needs_a_body(tmp_path) -> None:
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work = make_article(client, sections=0)
        got = client.post(f"/studio/works/{work['id']}/digest", json={})
        assert got.status_code == 400
        assert gw.completed == [], "没正文就不该调模型"


def test_digest_is_trimmed_to_the_limit(tmp_path) -> None:
    gw = FakeGateway("摘要" * 100)
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        got = client.post(f"/studio/works/{work['id']}/digest", json={})
        assert got.json()["chars"] == publish.DIGEST_MAX


# ==================== 迁移 ====================


async def test_digest_column_is_added_to_an_existing_database(tmp_path) -> None:
    """老库没有 digest 列：init 时要补上，否则写摘要直接炸。"""
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("SQLite 太老，不支持 DROP COLUMN")

    path = tmp_path / "old.db"
    first = StudioStore(path)
    await first.init()
    await first.close()

    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE works DROP COLUMN digest")
    conn.commit()
    conn.close()

    second = StudioStore(path)
    await second.init()
    try:
        cur = await second._db.execute("PRAGMA table_info(works)")
        cols = {row[1] for row in await cur.fetchall()}
        assert "digest" in cols
        work = await second.create_work(kind="article", title="旧库作品")
        assert await second.update_work(work["id"], digest="补上的摘要")
        assert (await second.get_work(work["id"]))["digest"] == "补上的摘要"
    finally:
        await second.close()


