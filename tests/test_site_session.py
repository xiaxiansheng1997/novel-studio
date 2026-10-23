"""站点会话隔离：URL -> 站点会话 id 提取、切换/新开路径，以及 history_mode 拼接。"""
from __future__ import annotations

from types import SimpleNamespace

from app.api.schemas import ChatCompletionRequest
from app.browser.site_session import extract_chat_id, open_conversation
from app.config import ProviderConfig
from app.providers._browser_base import BrowserStreamProvider


class _Loc:
    """够用的 locator 替身：count/click/first。"""

    def __init__(self, page: "_Page", hit: bool) -> None:
        self._page = page
        self._hit = hit
        self.first = self

    async def count(self) -> int:
        return 1 if self._hit else 0

    async def click(self, timeout: int = 0) -> None:  # noqa: ARG002 - 保持 Playwright 签名
        self._page.clicks.append("new-chat")
        self._page.url = self._page.new_chat_url


class _Page:
    def __init__(self, url: str, *, new_chat_url: str = "", clickable: bool = False) -> None:
        self.url = url
        self.new_chat_url = new_chat_url
        self.clickable = clickable
        self.gotos: list[str] = []
        self.clicks: list[str] = []
        self.waits = 0

    async def goto(self, url: str, **kwargs) -> None:  # noqa: ANN003, ARG002
        self.gotos.append(url)
        self.url = url

    async def wait_for_selector(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003, ARG002
        self.waits += 1

    def get_by_role(self, role: str, name: str | None = None) -> _Loc:  # noqa: ARG002
        return _Loc(self, self.clickable)

    def get_by_text(self, text: str, exact: bool = False) -> _Loc:  # noqa: ARG002
        return _Loc(self, self.clickable)


def _cfg(**kw) -> SimpleNamespace:
    base = {"key": "deepseek", "chat_url": "", "new_chat_labels": [], "home_url": "https://site/home"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_extract_chat_id_from_known_shapes() -> None:
    # 三家都按实测地址栏校准过：DeepSeek 短 id、千问 32 位 hex、豆包纯数字
    assert extract_chat_id("https://chat.deepseek.com/a/chat/s/abcd1234efgh") == "abcd1234efgh"
    assert (
        extract_chat_id("https://www.qianwen.com/chat/9a78ded52a9a429599c9f9f0acaddb90")
        == "9a78ded52a9a429599c9f9f0acaddb90"
    )
    assert extract_chat_id("https://www.doubao.com/chat/38447698537888002") == "38447698537888002"


def test_chat_url_template_roundtrip() -> None:
    """config 里填的模板拼出来的地址，必须还能被提取回同一个 id，否则切不回历史会话。"""
    cases = [
        ("https://www.qianwen.com/chat/{id}",
         "https://www.qianwen.com/chat/9a78ded52a9a429599c9f9f0acaddb90"),
        ("https://www.doubao.com/chat/{id}", "https://www.doubao.com/chat/38447698537888002"),
        ("https://chat.deepseek.com/a/chat/s/{id}", "https://chat.deepseek.com/a/chat/s/abcd1234efgh"),
    ]
    for tpl, url in cases:
        cid = extract_chat_id(url)
        assert cid, url
        assert tpl.replace("{id}", cid) == url


def test_extract_chat_id_ignores_non_conversation_pages() -> None:
    assert extract_chat_id("") is None
    assert extract_chat_id("https://www.doubao.com/chat/") is None
    assert extract_chat_id("https://www.qianwen.com/") is None
    assert extract_chat_id("https://site/settings") is None  # 固定路径段
    assert extract_chat_id("https://site/a/short") is None  # 太短，不像 id


async def test_reuse_when_already_on_target() -> None:
    page = _Page("https://chat.deepseek.com/a/chat/s/abcd1234efgh")
    res = await open_conversation(page, _cfg(chat_url="https://chat.deepseek.com/a/chat/s/{id}"), "abcd1234efgh")
    assert res["action"] == "reuse"
    assert page.gotos == []


async def test_navigate_with_template() -> None:
    page = _Page("https://site/")
    res = await open_conversation(page, _cfg(chat_url="https://site/c/{id}"), "xyz987654321")
    assert res["action"] == "navigate"
    assert page.gotos == ["https://site/c/xyz987654321"]
    assert page.waits >= 1


async def test_navigate_failure_falls_back_to_home() -> None:
    class _Boom(_Page):
        async def goto(self, url: str, **kwargs) -> None:  # noqa: ANN003, ARG002
            self.gotos.append(url)
            if "/c/" in url:
                raise RuntimeError("404")
            self.url = url

    page = _Boom("https://site/")
    res = await open_conversation(page, _cfg(chat_url="https://site/c/{id}"), "xyz987654321")
    assert res["action"] == "home"
    assert res["error"]  # 导航失败原因要带出来，便于排查
    assert page.gotos == ["https://site/c/xyz987654321", "https://site/home"]


async def test_new_chat_by_click_when_no_template() -> None:
    page = _Page("https://site/c/old99999999", new_chat_url="https://site/new", clickable=True)
    res = await open_conversation(page, _cfg(), None)
    assert res["action"] == "new"
    assert page.clicks == ["new-chat"]
    assert page.url == "https://site/new"


# ---- Provider 侧：映射落库 + history_mode 选路 ----


class _Store:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    async def get(self, conv_id: str, provider: str) -> str | None:
        return self.data.get((conv_id, provider))

    async def set(self, conv_id: str, provider: str, internal_id: str) -> None:
        self.data[(conv_id, provider)] = internal_id


class _Provider(BrowserStreamProvider):
    def parse_payload(self, payload: str):  # noqa: ANN201 - 测试不关心解析
        return None


def _provider(cfg: ProviderConfig, store: _Store) -> _Provider:
    ctx = SimpleNamespace(store=store, sessions=None, live=None, config=None)
    return _Provider(cfg, ctx)


def _req(**kw) -> ChatCompletionRequest:
    base = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "第一句"}, {"role": "assistant", "content": "回答"},
                     {"role": "user", "content": "第二句"}],
        "conversation_id": "conv1",
    }
    base.update(kw)
    return ChatCompletionRequest(**base)


async def test_remember_site_session_records_and_respects_failure() -> None:
    store = _Store()
    p = _provider(ProviderConfig(key="deepseek"), store)
    page = SimpleNamespace(url="https://chat.deepseek.com/a/chat/s/abcd1234efgh")

    await p._remember_site_session(_req(), page)
    assert store.data == {("conv1", "deepseek"): "abcd1234efgh"}

    # 切换失败时不能记：页面可能还停在别的站点会话上
    page2 = SimpleNamespace(url="https://chat.deepseek.com/a/chat/s/other999999")
    await p._remember_site_session(_req(), page2, allow=False)
    assert store.data == {("conv1", "deepseek"): "abcd1234efgh"}

    # 没会话 id / 页面不是会话页：都不写
    await p._remember_site_session(_req(conversation_id=None), page)
    await p._remember_site_session(_req(), SimpleNamespace(url="https://chat.deepseek.com/"))
    assert len(store.data) == 1


def test_build_prompt_switches_by_history_mode() -> None:
    store = _Store()
    site = _provider(ProviderConfig(key="deepseek", history_mode="site"), store)
    prompt = _provider(ProviderConfig(key="deepseek", history_mode="prompt"), store)

    # site：只发最后一句，上下文交给站点
    assert site.build_prompt(_req()) == "第二句"
    # prompt：system + 历史全部拼上（请求级覆盖优先）
    text = prompt.build_prompt(_req(history_mode=None, messages=_req().messages))
    assert "用户：第一句" in text and "助手：回答" in text and "用户：第二句" in text
    assert site.build_prompt(_req(history_mode="prompt")).startswith("用户：第一句")
