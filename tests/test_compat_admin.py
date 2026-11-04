"""直连通道页面配置（compat）：落库覆盖、掩码接口、即存即生效。

测试口径：config.yaml 与 storage 都挪进 tmp_path（monkeypatch BASE_DIR /
get_settings），绝不碰真库——这些用例会真写 sessions.sqlite 的
provider_compat 表，写进项目 storage/ 就是把用户的通道配置改了。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import config as config_mod
from app.api import admin_routes
from app.core import provider_store
from app.core.registry import Registry

CFG_TEXT = """
providers:
  qwen:
    enabled: false
    display_name: "通义千问"
    home_url: "https://example.com/"
  deepseek:
    enabled: true
    display_name: "DeepSeek"
    model_prefix: "deepseek-"
    home_url: "https://chat.deepseek.com/"
    models:
      - {id: deepseek-chat}
      - {id: deepseek-reasoner}
  openai:
    compat: true
    enabled: false
    display_name: "OpenAI 官方"
    base_url: "https://api.openai.com/v1"
    api_key_env: "OPENAI_API_KEY"
    model_prefix: "gpt-"
    models:
      - {id: gpt-4o-mini}
"""


@pytest.fixture
async def compat(tmp_path, monkeypatch):
    """tmp 里的 config.yaml + tmp 的 storage 目录 + 真 Registry + ASGI 客户端。"""
    (tmp_path / "config.yaml").write_text(CFG_TEXT, encoding="utf-8")
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    fake_settings = SimpleNamespace(
        resolved_storage_dir=lambda: tmp_path,
        api_key="", allow_origins="", host="127.0.0.1", port=8000,
        # 反代适配器模块导入时会读这两个开关（cdp_stream.py / _browser_base.py 模块级 get_settings）
        cdp_stream=False, stream_idle_timeout=120.0,
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake_settings)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_mod.get_config.cache_clear()

    cfg = config_mod.get_config()
    ctx = SimpleNamespace(config=cfg, sessions=None, store=None, live=None)
    reg = Registry(cfg, ctx)
    app = FastAPI()
    app.state.settings = fake_settings
    app.state.config = cfg
    app.state.registry = reg
    app.state.live = SimpleNamespace(config=cfg)

    # /admin/status 的行来自 SessionManager.status_all()，它遍历的是自己持的
    # config 引用——fixture 必须照抄这个行为，否则「保存后卡片列表立刻跟上」
    # 这条坑在这里测不出来（真进程实测就栽在这：漏递 sessions 新通道不显示）
    class _Sessions:
        def __init__(self, config):  # noqa: ANN001 - 测试内小桩
            self.config = config

        def status_all(self):
            return [
                {"provider": k, "display_name": p.display_name, "enabled": p.enabled,
                 "has_profile": not p.compat, "browser_open": False, "cached_at": None}
                for k, p in self.config.providers.items()
            ]

    app.state.sessions = _Sessions(cfg)
    app.include_router(admin_routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, reg, tmp_path
    config_mod.get_config.cache_clear()


def reload_cfg(tmp_path):  # noqa: ANN001 - 测试内助读：重读一次合并后的配置
    config_mod.get_config.cache_clear()
    return config_mod.get_config()


async def test_db_row_overrides_yaml(compat) -> None:
    """库里的行覆盖 yaml：base_url/key/模型逐字段生效，display_name 留空沿用 yaml。"""
    _client, _reg, tmp = compat
    provider_store.upsert_row(
        tmp, key="openai", display_name="", base_url="https://proxy.example/v1",
        api_key="sk-db-secret-9999", models=["gpt-4o", "gpt-4o-mini"], enabled=True,
    )
    pcfg = reload_cfg(tmp).providers["openai"]
    assert pcfg.base_url == "https://proxy.example/v1"
    assert pcfg.api_key == "sk-db-secret-9999"
    assert pcfg.model_ids() == ["gpt-4o", "gpt-4o-mini"]
    assert pcfg.enabled is True
    assert pcfg.display_name == "OpenAI 官方"


async def test_new_key_synthesizes_compat_entry(compat) -> None:
    """库里出现 yaml 没有的 key：合成 compat 条目（前缀 {key}-、不剥前缀），Registry 装直连适配器。"""
    _client, _reg, tmp = compat
    provider_store.upsert_row(
        tmp, key="deepseek-api", display_name="DeepSeek 官方", base_url="https://api.deepseek.com/v1",
        api_key="sk-ds-1234", models=["deepseek-chat"], enabled=True,
    )
    pcfg = reload_cfg(tmp).providers["deepseek-api"]
    assert pcfg.compat is True and pcfg.model_prefix == "deepseek-api-"
    assert pcfg.strip_prefix is False
    reg = Registry(config_mod.get_config(), SimpleNamespace())
    from app.providers.openai_compat import OpenAICompatAdapter
    assert isinstance(reg.adapters["deepseek-api"], OpenAICompatAdapter)


async def test_yaml_entry_has_no_api_key_field_leak(compat) -> None:
    """yaml 出厂值里没有库注入的 key：GET 一切接口只见掩码，明文不出后端。"""
    client, reg, tmp = compat
    provider_store.upsert_row(
        tmp, key="openai", display_name="OpenAI", base_url="https://a/v1",
        api_key="sk-supersecret-8890", models=["gpt-4o"], enabled=True,
    )
    # 直接写库不会走「保存即生效」链路，手动把新配置递进 registry（等效 PUT）
    reg.apply_config(reload_cfg(tmp))
    r = await client.get("/admin/compat")
    assert "sk-supersecret-8890" not in r.text
    row = next(p for p in r.json()["providers"] if p["provider"] == "openai")
    assert row["key_source"] == "db"
    assert row["key_masked"] == "…8890"
    assert row["in_yaml"] is True and row["in_db"] is True


async def test_put_partial_update_semantics(compat) -> None:
    """PUT：不传的字段保持原值；api_key 传空串 = 清除（回退 .env 通道，而 env 没设 → none）。"""
    client, _reg, tmp = compat
    r = await client.put("/admin/compat/openai", json={
        "base_url": "https://first/v1", "api_key": "sk-keepme-1234"})
    assert r.status_code == 200
    # 首次保存以 yaml 为底：模型清单没传也不能丢
    assert r.json()["models"] == ["gpt-4o-mini"]
    assert r.json()["base_url"] == "https://first/v1"

    r = await client.put("/admin/compat/openai", json={"enabled": False})
    j = r.json()
    assert j["base_url"] == "https://first/v1"   # 保持
    assert j["key_source"] == "db"               # key 保持
    assert j["enabled"] is False

    r = await client.put("/admin/compat/openai", json={"api_key": ""})
    assert r.json()["key_source"] == "none"      # env 变量声明了但没值


async def test_post_validates_key(compat) -> None:
    """新建：撞反代 key / 形状不对 → 400；重复 → 409；合法 → 立即可见且已生效。"""
    client, reg, tmp = compat
    assert (await client.post("/admin/compat", json={"key": "qwen"})).status_code == 400
    assert (await client.post("/admin/compat", json={"key": "Bad_Key"})).status_code == 400
    r = await client.post("/admin/compat", json={
        "key": "moonshot", "display_name": "Kimi", "base_url": "https://api.moonshot.cn/v1",
        "api_key": "sk-moon-7777", "models": ["moonshot-v1-8k"]})
    assert r.status_code == 200
    assert r.json()["in_yaml"] is False and r.json()["in_db"] is True
    # 保存即生效：不用重启，registry 里已经有这条适配器
    assert "moonshot" in reg.adapters
    assert (await client.post("/admin/compat", json={"key": "moonshot"})).status_code == 409


async def test_delete_reverts_to_yaml(compat) -> None:
    """删库行 = 回退 yaml 出厂值；库里本来没有的删不掉（404）。"""
    client, _reg, tmp = compat
    await client.put("/admin/compat/openai", json={"base_url": "https://gone/v1"})
    r = await client.delete("/admin/compat/openai")
    assert r.status_code == 200 and r.json()["reverted_to_yaml"] is True
    assert reload_cfg(tmp).providers["openai"].base_url == "https://api.openai.com/v1"
    assert (await client.delete("/admin/compat/openai")).status_code == 404


async def test_status_flags_compat_rows(compat) -> None:
    """/admin/status 给 compat 行补直连字段，反代行不带——前端卡片分支的开关就在这。"""
    client, _reg, tmp = compat
    await client.put("/admin/compat/openai", json={"api_key": "sk-status-4321"})
    rows = {p["provider"]: p for p in (await client.get("/admin/status")).json()["providers"]}
    assert rows["openai"]["compat"] is True
    assert rows["openai"]["key_source"] == "db"
    assert rows["openai"]["key_masked"] == "…4321"
    assert rows["openai"]["ready"] is True   # base_url+key+模型齐，enabled 也开了
    assert rows["qwen"]["compat"] is False
    assert "base_url" not in rows["qwen"]
    assert "sk-status-4321" not in json.dumps(rows)


async def test_new_channel_appears_in_status_immediately(compat) -> None:
    """新建通道后 /admin/status 马上有新行：SessionManager 和 LiveBrowserManager
    一样持着旧 config 引用，_apply_runtime_config 漏递谁，谁的画面就到重启前
    都冻在旧名单上——页面保存看着成功、卡片不出来就是这一条。"""
    client, _reg, _tmp = compat
    r = await client.post("/admin/compat", json={
        "key": "local-test", "base_url": "http://localhost:11434/v1",
        "models": ["qwen2.5:7b"]})
    assert r.status_code == 200
    rows = {p["provider"]: p for p in (await client.get("/admin/status")).json()["providers"]}
    assert "local-test" in rows
    assert rows["local-test"]["compat"] is True and rows["local-test"]["ready"] is True


async def test_route_prefers_compat_over_broad_prefix(compat) -> None:
    """反代宽前缀（deepseek-）撞直连显式 id（deepseek-flash）时，路由必须按
    显式声明判——真机实测栽过：单遍「谁先 matches 谁赢」被前缀劫走，
    UI 选的是直连模型、后端拉的是浏览器。同名 id 直连优先，合直连优先、
    反代兜底的口径；只存在于前缀空间的别名名（deepseek-think）仍走反代。"""
    client, reg, _tmp = compat
    r = await client.post("/admin/compat", json={
        "key": "deepseek-api", "display_name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1", "api_key": "sk-route-0001",
        "models": ["deepseek-flash", "deepseek-chat"]})
    assert r.status_code == 200
    from app.providers.deepseek import DeepSeekAdapter
    from app.providers.openai_compat import OpenAICompatAdapter
    assert isinstance(reg.route("deepseek-flash"), OpenAICompatAdapter)
    assert isinstance(reg.route("deepseek-chat"), OpenAICompatAdapter)
    assert isinstance(reg.route("deepseek-think"), DeepSeekAdapter)


async def test_adapter_db_key_beats_env(compat, monkeypatch) -> None:
    """取值优先级：库里的 cfg.api_key > 环境变量；库里没有才轮到 .env。"""
    _client, _reg, tmp = compat
    from app.providers.openai_compat import OpenAICompatAdapter
    pcfg = config_mod.ProviderConfig(key="x", compat=True, base_url="https://a/v1",
                                     api_key_env="OPENAI_API_KEY", api_key="sk-from-db")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    adapter = OpenAICompatAdapter(pcfg, SimpleNamespace())
    assert adapter._api_key() == "sk-from-db"
    pcfg2 = config_mod.ProviderConfig(key="x", compat=True, base_url="https://a/v1",
                                      api_key_env="OPENAI_API_KEY")
    assert OpenAICompatAdapter(pcfg2, SimpleNamespace())._api_key() == "sk-from-env"


async def test_test_endpoint_reports_upstream(compat, monkeypatch) -> None:
    """连通性体检：200 → ok；401 → 把上游状态与原文带回来（不 500，体检失败也是有效信息）。"""
    client, _reg, tmp = compat
    await client.put("/admin/compat/openai", json={
        "base_url": "https://api.test/v1", "api_key": "sk-probe-1", "enabled": True})

    real = httpx.AsyncClient  # 打补丁前拿住原类：补丁后 httpx.AsyncClient 就是 make 自己，再引用会套娃

    def _factory(handler):
        def make(**kwargs):  # 路由里 AsyncClient(timeout=...) 的构造换成带 MockTransport 的
            return real(transport=httpx.MockTransport(handler))
        return make

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient",
                        _factory(lambda req: httpx.Response(200, json={"choices": []})))
    r = await client.post("/admin/compat/openai/test", json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    # 裸 POST（完全不发 body）也要能测：测已存配置是常态用法，422 是添堵
    r = await client.post("/admin/compat/openai/test")
    assert r.status_code == 200 and r.json()["ok"] is True

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient",
                        _factory(lambda req: httpx.Response(401, json={"error": {"message": "bad key"}})))
    r = await client.post("/admin/compat/openai/test", json={"api_key": "sk-wrong"})
    assert r.json()["ok"] is False and "401" in r.json()["detail"]
