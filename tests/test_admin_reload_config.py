"""/admin/reload 必须把新 config 递到 LiveBrowserManager 手上。

背景：`LiveBrowserManager(pool, config)` 在构造时就持住了那个 config 对象，而
`registry.apply_config()` 只更新 registry / ctx / 各 adapter.cfg。少递一步的话，
/live 建页面读的还是旧的 `headless`，后台聊天读的却是新的 —— 两条链路都调
`pool.context(key, headless=...)`，谁先建谁定，改 `headless` 看着像没生效。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.api import admin_routes
from app.config import get_config


class _FakeRegistry:
    def __init__(self) -> None:
        self.applied = None

    def apply_config(self, config):  # noqa: ANN001 - 与真实 Registry 同签名
        self.applied = config
        return {"added": [], "updated": [], "removed": []}

    def get(self, key):  # noqa: ARG002 - added 为空，不会被调到
        return None


def _make_state():
    return SimpleNamespace(
        config=None,
        registry=_FakeRegistry(),
        live=SimpleNamespace(config=SimpleNamespace(sentinel="old")),
    )


async def test_reload_propagates_config_to_live_manager() -> None:
    state = _make_state()
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    get_config.cache_clear()

    res = await admin_routes.reload_config(request)

    assert res["reloaded"] is True
    assert state.config is state.registry.applied
    # 这一句就是回归点：live 拿到的必须是同一个新对象，而不是构造时那个旧的
    assert state.live.config is state.registry.applied
    assert getattr(state.live.config, "sentinel", None) != "old"


async def test_reload_survives_state_without_live() -> None:
    """单测/脚本里装配的 state 可能没有 live（只有 HTTP 服务才有），不能因此 500。"""
    state = _make_state()
    del state.live
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    get_config.cache_clear()

    res = await admin_routes.reload_config(request)
    assert res["reloaded"] is True


async def test_qwen_is_headed_and_throttled() -> None:
    """配好的两个反风控参数得真能被读到，否则改 yaml 改了个寂寞。"""
    get_config.cache_clear()
    pcfg = get_config().providers["qwen"]
    assert pcfg.headless is False, "千问要走有头，才能在真窗口里用真鼠标过滑块"
    assert pcfg.min_interval_sec >= 30, "相邻请求间隔太短会被 Baxia 计成机器行为"
