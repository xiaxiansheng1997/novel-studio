"""录制护栏：风控流量不许冒充对话接口写进 fixture。

起因是实测踩到的坑：千问被阿里云 Baxia 拦下时，页面上只剩
`/api/v2/chat/_____tmd_____/report` 这类流量，它既命中 URL_HINTS 的 "chat"（拦截路径
就长在 chat 下面），body 里又整张塞着一张 canvas 指纹图（base64 WebP），体积碾压真实
对话响应 —— 于是 pick_endpoint 把冠军判给了它，写出的 fixture 拿去回放完全跑错方向。

护栏装在录制入口（而不是只装在打分里）：candidates 也得保持干净，否则下次看着录制结果
照样会被误导。
"""
from __future__ import annotations

import asyncio
import json

from app.browser import network_recorder as nr

# 用户实测录制里真实出现过的三条风控流量
RISK_URLS = [
    "https://chat2.qianwen.com/api/v2/chat/_____tmd_____/report",
    "https://chat2.qianwen.com/api/v2/chat/_____tmd_____/slide?slidedata=%7B%22a%22%3A%22X82Y",
    "https://chat2.qianwen.com//api/v2/chat/_____tmd_____/punish?x5secdata=xgd153b50c4aba",
]
REAL_URLS = [
    "https://chat2.qianwen.com/api/v2/chat",
    "https://chat2.qianwen.com/api/v2/chat/stream",
    "https://chat.deepseek.com/api/v0/chat/completion",
    "https://www.doubao.com/completion/stream_call_bot",
]


class _FakeReq:
    def __init__(self, resource_type: str = "fetch", method: str = "POST") -> None:
        self.resource_type = resource_type
        self.method = method
        self.headers: dict = {}
        self.post_data = "{}"


class _FakeResp:
    def __init__(self, url: str, req: _FakeReq | None = None, sample: str = "{}") -> None:
        self.url = url
        self.request = req or _FakeReq()
        self.status = 200
        self.headers = {"content-type": "application/json;charset=UTF-8"}
        self._sample = sample

    async def text(self) -> str:
        return self._sample


class _FakePage:
    """只实现 NetworkRecorder 用到的那半个 Playwright page 接口。"""

    def __init__(self) -> None:
        self.handler = None

    def on(self, event: str, cb) -> None:
        if event == "response":
            self.handler = cb

    def remove_listener(self, event: str, cb) -> None:  # noqa: ARG002 - 与 Playwright 同签名
        self.handler = None


async def _feed(page: _FakePage, *resps: _FakeResp) -> None:
    for r in resps:
        page.handler(r)
    # handler 内部用 ensure_future 派发，让事件循环把这些协程跑完
    for _ in range(20):
        await asyncio.sleep(0)


def test_risk_urls_blocked_and_real_ones_pass() -> None:
    assert all(nr.is_risk_control(u) for u in RISK_URLS)
    assert not any(nr.is_risk_control(u) for u in REAL_URLS)


def test_tmd_report_really_would_have_won_the_pick() -> None:
    """先证明这个坑是真的：没有护栏时，体积最大的风控上报确实会抢到端点。"""
    entries = [
        {"url": REAL_URLS[0], "method": "POST", "response_content_type": "application/json",
         "response_sample": "{}", "post_data": "{}"},
        {"url": RISK_URLS[0], "method": "POST", "response_content_type": "application/json",
         "response_sample": "x" * 4000, "post_data": "result=data:image/webp;base64,UklGR"},
    ]
    assert nr.pick_endpoint("qwen", entries)["url"] == RISK_URLS[0]


async def test_attach_drops_risk_traffic_and_counts_it() -> None:
    page = _FakePage()
    rec = nr.NetworkRecorder()
    rec.attach(page)

    big = "x" * 4000
    await _feed(
        page,
        _FakeResp(RISK_URLS[0], sample=big),
        _FakeResp(RISK_URLS[2], sample=big),
        _FakeResp(REAL_URLS[0], sample='{"ok":true}'),
    )

    assert [e["url"] for e in rec.entries] == [REAL_URLS[0]], "风控流量不该进 entries"
    assert rec.skipped_risk == 2
    assert all(not nr.is_risk_control(c["url"]) for c in rec.entries)


async def test_non_xhr_traffic_is_not_counted_as_risk() -> None:
    """静态资源是被原有的 resource_type 闸门拦的，不能被算进「排除了多少条风控」。"""
    page = _FakePage()
    rec = nr.NetworkRecorder()
    rec.attach(page)
    await _feed(page, _FakeResp("https://g.alicdn.com/baxia/common.js", _FakeReq("script")))
    assert rec.entries == []
    assert rec.skipped_risk == 0


async def test_only_risk_traffic_leaves_fixture_untouched(tmp_path, monkeypatch) -> None:
    """一条对话都没发出去时，绝不写文件覆盖掉上次的好 fixture。"""
    monkeypatch.setattr(nr, "FIXTURES_DIR", tmp_path)
    dest = tmp_path / "qwen.json"
    before = json.dumps({"endpoint": REAL_URLS[0]}, ensure_ascii=False)
    dest.write_text(before, encoding="utf-8")

    rec = nr.NetworkRecorder()
    rec.attach(_FakePage())
    rec.skipped_risk = 5

    res = rec.write_fixture("qwen")
    assert res["skipped"] is True
    assert res["file"] is None
    assert res["risk_skipped"] == 5
    assert "风控" in res["message"]
    assert dest.read_text(encoding="utf-8") == before


async def test_empty_recording_message_does_not_claim_risk(tmp_path, monkeypatch) -> None:
    """什么都没抓到（压根没发请求）时，提示得是「发一句对话再重录」而不是甩锅给风控。"""
    monkeypatch.setattr(nr, "FIXTURES_DIR", tmp_path)
    rec = nr.NetworkRecorder()
    rec.attach(_FakePage())

    res = rec.write_fixture("qwen")
    assert res["skipped"] is True
    assert res["risk_skipped"] == 0
    assert "风控" not in res["message"]
