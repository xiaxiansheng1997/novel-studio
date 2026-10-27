"""交互式生成的指令口径：提示词留空不等于没指令。

症状是「选单篇、进写作台、下方框子什么都不写、点生成」——出来的东西像没接到任务：
`/studio/nodes/{id}/generate` 原来是把 `body.instruction` **裸传**给装配器的，
而「大纲 / 整篇 / 排队」那三条路都先过一遍 `prompting.node_instruction()`，
把「本节标题 / 本节要点 / 篇幅 / 只输出本节正文」包进去（见 `app/studio/tasks.py`）。
裸传时只要框子为空，S8 这一整块就不出现（`_head("")` 给的是空文本，拼装时按空跳过），
模型只剩模板骨架与主旨，连要写哪一节、写多少字都不知道。

同一个坑还有一半在「润色」上：polish 桶原来只有 S0/S1/S7/S8，看不见 S2 作品圣经
也看不见 S_src 素材，于是模型改完语言会把主旨里定好的口径写偏，还没处核对数字与专名。
补上桶以后，「该作品还没有素材，生成质量会明显下降」这句会每次润色都弹出来——
而润色不引入新事实，这句只对写新内容的人有意义，所以告警也跟着收窄到起草类任务。

用例分两层：装配层直接调 `assemble`（桶与告警是纯本地逻辑），
接口层全程走 HTTP（aiosqlite 的连接绑在建立它的事件循环上，不能让 store 在 pytest 循环里初始化）。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import studio_routes
from app.studio import assembler, content_types
from app.studio.prompting import node_instruction
from app.studio.store import StudioStore

BODY = "把长文拆成能独立读的一节一节，这一节讲怎么起标题。" * 20


class FakeGateway:
    """记下每次真正发出去的 system/prompt，全程不打网络。"""

    def __init__(self, reply: str = "模型写好的段落。") -> None:
        self.reply = reply
        self.streamed: list[dict] = []
        self.completed: list[dict] = []

    def resolve(self, model: str):  # 路由只取 [0] 当 provider 名记账
        return ("fake", "")

    async def stream(self, *, model: str, system: str, prompt: str, mode: str = "quick"):
        self.streamed.append({"model": model, "system": system, "prompt": prompt})
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
    # pick_model 只要 route() 不抛异常就认这个模型；adapters 留空不影响
    app.state.registry = SimpleNamespace(route=lambda name: (name, {}), adapters={})
    app.include_router(studio_routes.router)
    return TestClient(app)


def make_article(client: TestClient, *, with_body: bool = False) -> tuple[dict, dict]:
    """一本单篇作品 + 一个节；`with_body` 时节里已有正文（润色的前提）。"""
    work = client.post("/studio/works", json={
        "kind": "article", "scale": "single", "title": "单篇测试",
        "premise": "把长文拆成能独立读的节",
    }).json()
    node = client.post(f"/studio/works/{work['id']}/nodes", json={
        "kind": "section", "title": "怎么起标题", "content": BODY if with_body else "",
    }).json()
    return work, node


# ==================== 装配层：润色也看得见主旨与素材 ====================

async def test_polish_sees_bible_and_sources(store) -> None:
    """润色不能只看见正文：S2 的主旨与 S_src 的素材都得在场，否则改语言会顺手改口径。"""
    work = await store.create_work(kind="article", scale="single", title="润色上下文",
                                   premise="只讲事实，不写营销话")
    node = await store.create_node(work_id=work["id"], kind="section", title="第一节",
                                   content=BODY, ordinal=1)
    await store.add_source(work["id"], content="2026 年公众号打开率降到 1.3%。",
                           title="打开率数据")

    res = await assembler.assemble(store, work, node, "polish", "把第三段压短")

    ids = [b.id for b in res.buckets]
    assert "S2" in ids and "S_src" in ids, ids
    assert "只讲事实，不写营销话" in res.user
    assert "打开率数据" in res.user
    assert "把第三段压短" in res.user


async def test_missing_sources_warns_only_for_draft_tasks(store) -> None:
    """没素材那句告警只对写新内容的人说：润色不动事实，每次弹一句只会让人误以为得先停下导资料。"""
    work = await store.create_work(kind="article", scale="single", title="无素材", premise="x")
    node = await store.create_node(work_id=work["id"], kind="section", title="第一节",
                                   content=BODY, ordinal=1)

    draft = await assembler.assemble(store, work, node, "article_draft", "写这一节")
    assert any("生成质量会明显下降" in w for w in draft.warnings), draft.warnings

    polish = await assembler.assemble(store, work, node, "polish", "顺一遍语言")
    assert not any("明显下降" in w for w in polish.warnings), polish.warnings
    assert "S_src" not in [b.id for b in polish.buckets], "没素材就不该占一个空桶"


# ==================== 接口层：交互式生成与整篇同一份指令 ====================

def test_generate_wraps_instruction_when_box_is_empty(tmp_path) -> None:
    """框子留空点生成，模型仍要拿到「本节标题 / 要点 / 篇幅 / 只输出本节正文」。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work, node = make_article(client)
        meta = {"outline": {node["id"]: {"prompt": "给出 5 个备选并说明各自适用场景",
                                        "target_words": 1200}}}
        assert client.patch(f"/studio/works/{work['id']}",
                            json={"meta_json": json.dumps(meta, ensure_ascii=False)}).status_code == 200

        resp = client.post(f"/studio/nodes/{node['id']}/generate",
                           json={"task_type": "article_draft", "instruction": ""})
        assert resp.status_code == 200, resp.text

        sent = gw.streamed[0]["prompt"]
        assert "本节标题：怎么起标题" in sent
        assert "篇幅：约 1200 字" in sent
        assert "只输出本节正文" in sent
        # 与队列/整篇共用同一个拼装函数：这里比的是「一模一样」，不是「看起来差不多」
        fresh = client.get(f"/studio/nodes/{node['id']}").json()
        assert node_instruction(fresh, meta, "") in sent


def test_generate_keeps_the_user_line_as_extra(tmp_path) -> None:
    """写了提示词就当「附加要求」并进同一条指令，不是替换掉本节标题与篇幅那几句。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        _, node = make_article(client)
        client.post(f"/studio/nodes/{node['id']}/generate",
                    json={"task_type": "article_draft", "instruction": "结尾加一句互动"})

        sent = gw.streamed[0]["prompt"]
        assert "附加要求：结尾加一句互动" in sent
        assert "本节标题：怎么起标题" in sent


def test_context_preview_shows_what_is_actually_sent(tmp_path) -> None:
    """右栏预览必须显示真要发出去的那一份，否则照着预览调 prompt 是白调。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work, node = make_article(client)
        meta = {"outline": {node["id"]: {"prompt": "五个备选加适用场景", "target_words": 800}}}
        client.patch(f"/studio/works/{work['id']}",
                     json={"meta_json": json.dumps(meta, ensure_ascii=False)})

        got = client.post(f"/studio/context/preview?work_id={work['id']}&node_id={node['id']}",
                          json={"task_type": "article_draft", "instruction": ""})
        assert got.status_code == 200, got.text
        assert "本节标题：怎么起标题" in got.json()["user"]


def test_polish_needs_an_existing_body(tmp_path) -> None:
    """空节选「润色」要当场拦住：装配器只会看到骨架，模型就凭空编出一节来。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        _, empty = make_article(client)
        bad = client.post(f"/studio/nodes/{empty['id']}/generate",
                          json={"task_type": "polish", "instruction": "顺一遍"})
        assert bad.status_code == 400
        assert "润色需要已有正文" in bad.json()["detail"]
        assert gw.streamed == [], "拦截发生在调模型之前"

        _, filled = make_article(client, with_body=True)
        ok = client.post(f"/studio/nodes/{filled['id']}/generate",
                         json={"task_type": "polish", "instruction": "顺一遍"})
        assert ok.status_code == 200, ok.text


def test_adopted_polish_is_not_recorded_as_a_draft(tmp_path) -> None:
    """落库的版本来源按任务标：历史页里「AI 润色」与「AI 草稿」得能分开看。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        _, node = make_article(client, with_body=True)
        client.post(f"/studio/nodes/{node['id']}/generate",
                    json={"task_type": "polish", "instruction": "顺一遍", "save": True})

        rows = client.get(f"/studio/nodes/{node['id']}/versions").json()
        assert rows[0]["source"] == "ai_polish", rows


def test_patch_node_accepts_a_known_source_only(tmp_path) -> None:
    """`snapshot_source` 是白名单：放开成任意字符串，历史页会出现没人看得懂的行。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        _, node = make_article(client, with_body=True)
        assert client.patch(f"/studio/nodes/{node['id']}",
                            json={"content": "改了一版", "snapshot": True,
                                  "snapshot_source": "ai_polish"}).status_code == 200
        rows = client.get(f"/studio/nodes/{node['id']}/versions").json()
        assert rows[0]["source"] == "ai_polish"

        bad = client.patch(f"/studio/nodes/{node['id']}",
                           json={"content": "再改一版", "snapshot": True,
                                 "snapshot_source": "gpt_said_so"})
        assert bad.status_code == 400
        assert "版本来源不认" in bad.json()["detail"]
        # 手改不传来源，仍落 human
        client.patch(f"/studio/nodes/{node['id']}", json={"content": "手工改", "snapshot": True})
        rows = client.get(f"/studio/nodes/{node['id']}/versions").json()
        assert rows[0]["source"] == "human"


def test_work_title_and_premise_are_editable_after_creation(tmp_path) -> None:
    """建完作品才想换标题/补主旨是常事：这条得走 PATCH，S2 里那句主旨每轮生成都看得见。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        work, _ = make_article(client)
        got = client.patch(f"/studio/works/{work['id']}",
                           json={"title": "换了的标题", "premise": "重写过的主旨"}).json()
        assert got["title"] == "换了的标题" and got["premise"] == "重写过的主旨"
        assert client.get(f"/studio/works/{work['id']}").json()["title"] == "换了的标题"


# ==================== 起标题 ====================

def test_transform_title_action_asks_for_five_candidates(tmp_path) -> None:
    """起标题不是「加工一段文字」：输出格式先说死，否则默认那句会把标题写成一段散文。"""
    gw = FakeGateway(reply="标题一\n标题二\n标题三\n标题四\n标题五")
    with make_client(tmp_path, gw) as client:
        resp = client.post("/studio/transform", json={
            "text": BODY, "action": "title", "instruction": "别用感叹号",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["label"] == "起标题"

        sent = gw.completed[0]
        assert sent["system"].startswith("你是公众号编辑。")
        assert "5 个备选标题" in sent["prompt"]
        assert "别用感叹号" in sent["prompt"]
        assert len(resp.json()["candidates"]) == 1

        assert client.post("/studio/transform", json={"text": "x", "action": "没有这个动作"}
                           ).status_code == 400


def test_transform_default_system_stays_for_text_actions(tmp_path) -> None:
    """除 title 之外都还是「只输出加工后的正文」，别把改写也带成一行一行。"""
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        client.post("/studio/transform", json={"text": BODY, "action": "polish"})
        assert gw.completed[0]["system"].startswith("你是中文编辑，")


# ==================== 层级标签外露（前端建节点与空态文案跟着真实层级走） ====================

def test_content_types_expose_layer_labels(tmp_path) -> None:
    gw = FakeGateway()
    with make_client(tmp_path, gw) as client:
        kinds = client.get("/studio/content-types").json()["kinds"]
        article = next(k for k in kinds if k["id"] == "article")
        by_id = {s["id"]: s for s in article["scales"]}

        # 单篇只有一层「节」：没有容器层，前端就不该写「先建卷/章」
        assert by_id["single"]["leaf_kind"] == "section"
        assert by_id["single"]["leaf_label"] == "节"
        assert by_id["single"]["container_kind"] is None
        assert by_id["single"]["container_label"] is None
        # 系列多一层「讲」
        assert by_id["series"]["container_kind"] == "volume"
        assert by_id["series"]["container_label"] == "讲"


def test_resolve_defaults_are_part_of_the_contract() -> None:
    """直接调 resolve 的脚本/测试也该拿到这四个键，缺键会让前端拼出 undefined。"""
    resolved = content_types.resolve("article", "single", "")
    assert {"leaf_kind", "leaf_label", "container_kind", "container_label"} <= set(resolved)
    assert resolved["container_kind"] is None
