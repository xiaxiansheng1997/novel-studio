"""公众号第二轮：文风画像、版式、文末、换标题、转发语、发布台账。

这一组用例守的是「发出去之前」的四个判断：
1. 文风到底有没有进 prompt（作者贴了旧文却不起作用，是最难自己发现的失败）
2. 版式/文末/往期是真的会改导出结果，而不是只存在设置里
3. 发布检查能抓到外链、套话开头、通篇不加粗
4. 台账能聚合出书架要的数字，往期只取带链接的那一条
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import publish, voice, wechat
from app.studio.assembler import assemble
from app.studio.store import StudioStore

SAMPLE = (
    "其实我一直觉得，写公众号最难的不是写，是删。\n\n"
    "你看，一篇文章写成三千字很容易。删到一千八，难。\n\n"
    "你发现没有？那些读完率高的号，段落都很短。一句话一段，读起来不累。\n\n"
    "说白了，手机屏幕就那么窄。你一屏塞三段，读者手指一滑就走了。"
)
BODY = (
    "先说一个具体场景。\n\n"
    "**判断**：这件事值得做。\n\n"
    "[图：一张曲线图]\n\n"
    "你会怎么选？留言聊聊。"
)


class FakeGateway:
    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.completed: list[dict] = []

    def resolve(self, model: str):
        return ("fake", "")

    async def stream(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        yield SimpleNamespace(text=self.reply)

    async def complete(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        self.completed.append({"system": system, "prompt": prompt})
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


def make_article(client: TestClient, *, sections: int = 2, sub_type: str = "wechat") -> dict:
    work = client.post("/studio/works", json={
        "kind": "article", "scale": "single", "subtype": sub_type,
        "title": "公众号第二轮", "premise": "测试",
    }).json()
    for i in range(sections):
        client.post(f"/studio/works/{work['id']}/nodes", json={
            "kind": "section", "title": f"第 {i + 1} 节", "content": BODY,
        })
    return work


# ==================== 文风统计 ====================


def test_voice_counts_sentences_paragraphs_and_person() -> None:
    stats = voice.analyze([SAMPLE])

    # 4 段、每段 1-2 句；短句为主
    assert stats["paragraphs"] == 4
    assert stats["sentences"] >= 4
    assert stats["sent_avg"] < voice.SHORT_SENT
    # 样本里「你」远多于「我」
    assert stats["you_per_k"] > stats["i_per_k"] > 0
    assert stats["q_per_k"] > 0


def test_voice_profile_says_only_what_the_numbers_support() -> None:
    got = voice.build([SAMPLE])

    assert got.startswith("文风画像（1 篇旧文 /")
    assert "- 句子：平均" in got
    assert "短句为主" in got
    # 样本里没有破折号、没有感叹号：这两项不该出现在画像里凑字数
    assert "破折号" not in got
    assert "感叹号" not in got


def test_voice_empty_input_is_empty_profile() -> None:
    assert voice.build([]) == ""
    assert voice.build(["   \n\n  "]) == ""


def test_voice_ignores_code_blocks() -> None:
    """样本里贴了代码，句长统计会被拉偏——代码块整段不参与。"""
    with_code = "一句话。第二句。\n\n```python\nprint('x' * 100)\n```"
    stats = voice.analyze([with_code])
    assert stats["sentences"] == 2
    assert stats["sent_avg"] <= 5


async def test_voice_profile_reaches_s1(tmp_path) -> None:
    store = StudioStore(tmp_path / "s.db")
    await store.init()
    try:
        await store.set_setting("voice_profile", "文风画像（1 篇旧文 / 900 字）\n- 句子：平均 12 字")
        work = await store.create_work(kind="article", title="探针", subtype="wechat")
        res = await assemble(store, work, None, "article_draft", "写一段")
        s1 = next(b for b in res.buckets if b.id == "S1")
        assert "平均 12 字" in s1.text
        assert "按作者自己的文风写" in s1.text
    finally:
        await store.close()


async def test_no_profile_means_no_noise(tmp_path) -> None:
    store = StudioStore(tmp_path / "s.db")
    await store.init()
    try:
        work = await store.create_work(kind="article", title="探针", subtype="wechat")
        res = await assemble(store, work, None, "article_draft", "写一段")
        s1 = next(b for b in res.buckets if b.id == "S1")
        assert "按作者自己的文风写" not in s1.text
    finally:
        await store.close()


# ==================== 版式与文末 ====================


def test_themes_change_the_look_but_keep_the_text() -> None:
    secs = [{"title": "节", "content": "正文一句话。"}]
    paper = wechat.render_article(secs, theme="paper")
    mono = wechat.render_article(secs, theme="mono")
    warm = wechat.render_article(secs, theme="warm")

    assert paper != mono and mono != warm
    assert "#8b5e3c" in warm, "暖色版的标题色要真的写进内联样式"
    for page in (paper, mono, warm):
        assert "正文一句话。" in page
    # 主题名写错不该让导出失败：退回默认
    assert wechat.render_article(secs, theme="没有这个") == paper


def test_footer_and_archive_are_optional() -> None:
    secs = [{"title": "节", "content": "正文"}]
    got = wechat.render_article(
        secs, footer="觉得有用，点个关注。",
        archive=[{"title": "往期一篇", "url": "https://mp.weixin.qq.com/s/old"}],
    )
    assert "点个关注" in got
    assert "往期推荐" in got and "s/old" in got
    # 连不上链接的往期条目不该占位置
    only_bad = wechat.render_article(secs, archive=[{"title": "没链接"}])
    assert "往期推荐" not in only_bad
    assert "点个关注" not in wechat.render_article(secs)


# ==================== 发布检查新增四条 ====================


def test_check_flags_external_links_and_cliche_opening() -> None:
    work = {"title": "标题"}
    sections = [{
        "id": "n1", "title": "开头",
        "content": "随着移动互联网的发展，我们都需要一篇公众号。详情见 https://example.com/x 。",
    }]
    items = publish.check(work, sections, digest="一段合规的摘要。")
    rules = {i["rule"] for i in items}

    assert "external_link" in rules
    hit = next(i for i in items if i["rule"] == "external_link")
    assert "点不动" in hit["message"]
    assert "weak_opening" in rules
    opening = next(i for i in items if i["rule"] == "weak_opening")
    assert "随着" in opening["message"]


def test_check_flags_a_long_post_without_a_single_bold_sentence() -> None:
    work = {"title": "标题"}
    body = "这是一句正常长度的话。" * 120  # 1440 字，一处加粗都没有
    items = publish.check(work, [{"id": "n1", "title": "节", "content": body}], digest="摘要")
    assert "no_bold" in {i["rule"] for i in items}


def test_check_flags_an_overlong_heading() -> None:
    work = {"title": "标题"}
    sections = [{
        "id": "n1", "title": "这是一个特别长的小标题一眼看去像正文超过了二十个字",
        "content": "正文。" * 20,
    }]
    rules = {i["rule"] for i in publish.check(work, sections, digest="摘要")}
    assert "heading_too_long" in rules


def test_check_does_not_nag_a_short_clean_post_about_bold() -> None:
    work = {"title": "短稿"}
    sections = [
        {"id": "n1", "title": "开头", "content": "先说一个具体场景。"},
        {"id": "n2", "title": "收尾", "content": "你会怎么选？留言聊聊。"},
    ]
    rules = {i["rule"] for i in publish.check(work, sections, digest="一段摘要")}
    assert "no_bold" not in rules
    assert "weak_opening" not in rules


# ==================== 导出接口 ====================


def test_export_applies_theme_footer_and_archive(tmp_path) -> None:
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        other = client.post("/studio/works", json={
            "kind": "article", "scale": "single", "subtype": "wechat", "title": "往期一篇",
        }).json()
        client.post(f"/studio/works/{other['id']}/publications", json={
            "published_at": "2026-09-01", "url": "https://mp.weixin.qq.com/s/old", "reads": 800,
        })
        client.put("/studio/settings/footer_text", json={"value": "觉得有用，点个关注。"})
        client.put("/studio/settings/footer_archive", json={"value": "1"})

        html = client.get(f"/studio/works/{work['id']}/export?format=html&theme=warm").text
        assert "#8b5e3c" in html
        assert "点个关注" in html
        assert "往期推荐" in html and "s/old" in html

        bare = client.get(
            f"/studio/works/{work['id']}/export?format=html&footer=false&archive=false"
        ).text
        assert "点个关注" not in bare
        assert "往期推荐" not in bare


def test_publish_check_reports_the_theme_and_footer_state(tmp_path) -> None:
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        client.put("/studio/settings/theme", json={"value": "mono"})
        data = client.get(f"/studio/works/{work['id']}/publish-check").json()
        assert data["theme"] == "mono"
        assert set(data["themes"]) == {"paper", "mono", "warm"}
        assert data["title"] == "公众号第二轮"
        assert data["archive_on"] is False


# ==================== 文风接口 ====================


def test_voice_sample_rebuilds_the_profile(tmp_path) -> None:
    with make_client(tmp_path, FakeGateway()) as client:
        got = client.post("/studio/voice/samples", json={"title": "旧文", "content": SAMPLE})
        assert got.status_code == 200, got.text
        assert "文风画像" in got.json()["profile"]

        listed = client.get("/studio/voice").json()
        assert len(listed["samples"]) == 1
        assert listed["chars"] > 100

        assert client.delete(f"/studio/voice/samples/{listed['samples'][0]['id']}").status_code == 200
        assert client.get("/studio/voice").json()["profile"] == ""


def test_voice_sample_too_short_is_rejected(tmp_path) -> None:
    with make_client(tmp_path, FakeGateway()) as client:
        got = client.post("/studio/voice/samples", json={"content": "太短了"})
        assert got.status_code == 422


def test_manual_profile_wins_until_samples_change(tmp_path) -> None:
    with make_client(tmp_path, FakeGateway()) as client:
        client.put("/studio/voice/profile", json={"profile": "手写的画像"})
        assert client.get("/studio/voice").json()["profile"] == "手写的画像"
        client.post("/studio/voice/samples", json={"content": SAMPLE})
        assert client.get("/studio/voice").json()["profile"] != "手写的画像"


def test_unknown_setting_is_rejected(tmp_path) -> None:
    with make_client(tmp_path, FakeGateway()) as client:
        assert client.put("/studio/settings/nope", json={"value": "x"}).status_code == 400
        assert client.put("/studio/settings/footer_text", json={"value": "ok"}).status_code == 200


# ==================== 换标题 / 转发语 ====================


def test_titles_are_cleaned_and_counted(tmp_path) -> None:
    gw = FakeGateway("1. 标题甲\n- 标题乙\n3. 说明：\n只有三个字的")
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        got = client.post(f"/studio/works/{work['id']}/titles", json={})
        assert got.status_code == 200, got.text
        titles = got.json()["titles"]
        # 编号被去掉、以冒号结尾的解释行被丢掉
        assert [t["text"] for t in titles] == ["标题甲", "标题乙", "只有三个字的"]
        assert titles[0]["chars"] == 3
        assert "<text>" in gw.completed[-1]["prompt"]


def test_titles_need_a_body(tmp_path) -> None:
    gw = FakeGateway("标题甲")
    with make_client(tmp_path, gw) as client:
        work = make_article(client, sections=0)
        assert client.post(f"/studio/works/{work['id']}/titles", json={}).status_code == 400
        assert gw.completed == [], "没正文就不该调模型"


def test_share_copies_parse_json_and_fall_back(tmp_path) -> None:
    gw = FakeGateway(
        '[{"angle":"提问","text":"你写一篇要删几遍？"},{"angle":"结论","text":"删不掉是没想清"}]'
    )
    with make_client(tmp_path, gw) as client:
        work = make_article(client)
        copies = client.post(f"/studio/works/{work['id']}/share", json={}).json()["copies"]
        assert [c["angle"] for c in copies] == ["提问", "结论"]
        assert copies[0]["chars"] == len("你写一篇要删几遍？")

        # 模型不按 JSON 输出时退回按行切，接口不能因此 502
        gw.reply = "第一行文案\n第二行文案"
        copies = client.post(f"/studio/works/{work['id']}/share", json={}).json()["copies"]
        assert [c["text"] for c in copies] == ["第一行文案", "第二行文案"]


# ==================== 发布台账 ====================


def test_publication_crud_and_summary(tmp_path) -> None:
    with make_client(tmp_path, FakeGateway()) as client:
        work = make_article(client)
        got = client.post(f"/studio/works/{work['id']}/publications", json={
            "published_at": "2026-10-01", "url": "https://mp.weixin.qq.com/s/new", "reads": 1200,
        })
        assert got.status_code == 200, got.text
        pub = got.json()
        assert pub["reads"] == 1200

        listed = client.get(f"/studio/works/{work['id']}/publications").json()
        assert len(listed) == 1

        patched = client.patch(f"/studio/publications/{pub['id']}", json={"reads": 1500})
        assert patched.json()["reads"] == 1500

        summary = client.get("/studio/publications/summary").json()
        row = next(r for r in summary if r["work_id"] == work["id"])
        assert row["pubs"] == 1 and row["reads"] == 1500
        assert row["last_url"] == "https://mp.weixin.qq.com/s/new"

        assert client.delete(f"/studio/publications/{pub['id']}").status_code == 200
        assert client.get(f"/studio/works/{work['id']}/publications").json() == []


def test_archive_skips_the_current_work_and_empty_links(tmp_path) -> None:
    with make_client(tmp_path, FakeGateway()) as client:
        work = make_article(client)
        # 当前这篇自己也有一条：往期不应该把它列出来
        client.post(f"/studio/works/{work['id']}/publications", json={
            "published_at": "2026-10-09", "url": "https://mp.weixin.qq.com/s/self",
        })
        for title, url, day in (
            ("没链接", "", "2026-10-05"),
            ("往期", "https://mp.weixin.qq.com/s/old", "2026-09-01"),
        ):
            w = client.post("/studio/works", json={
                "kind": "article", "scale": "single", "subtype": "wechat", "title": title,
            }).json()
            client.post(f"/studio/works/{w['id']}/publications", json={
                "published_at": day, "url": url,
            })
        client.put("/studio/settings/footer_archive", json={"value": "1"})
        html = client.get(f"/studio/works/{work['id']}/export?format=html").text
        assert "s/old" in html
        assert "s/self" not in html
        assert "没链接" not in html


# ==================== 老库迁移 ====================


async def test_new_tables_are_created_on_an_existing_database(tmp_path) -> None:
    path = tmp_path / "old.db"
    first = StudioStore(path)
    await first.init()
    await first.close()

    conn = sqlite3.connect(path)
    for table in ("publications", "voice_samples", "settings"):
        conn.execute(f"DROP TABLE {table}")
    conn.commit()
    conn.close()

    second = StudioStore(path)
    await second.init()
    try:
        await second.set_setting("footer_text", "点个关注")
        assert await second.get_setting("footer_text") == "点个关注"
        sample = await second.add_voice_sample(content=SAMPLE, title="旧文")
        assert sample["chars"] > 100
        work = await second.create_work(kind="article", title="旧库作品")
        await second.add_publication(work["id"], url="https://mp.weixin.qq.com/s/x", reads=3)
        assert (await second.publication_summary())[0]["reads"] == 3
    finally:
        await second.close()