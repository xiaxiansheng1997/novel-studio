"""网络录制器：在真实页面上旁路记录对话请求，产出 fixtures/<key>.json。

被两处复用：
- scripts/recon.py（命令行兜底）
- 内嵌实时浏览器页面里的"录制"开关（live_routes）
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from loguru import logger

from app.config import BASE_DIR

FIXTURES_DIR = BASE_DIR / "fixtures"
RECON_SAMPLE_PROMPT = "你好"
MAX_ENTRIES = 500  # 长时间开录的内存上限
SAMPLE_CAP = 4000  # 单条响应样本截断长度

# 各家对话接口 url 的关键字线索（用于从众多请求里挑出最可能的那个）
URL_HINTS: dict[str, list[str]] = {
    "qwen": ["qianwen", "tongyi", "dialog", "conversation", "chat", "completion", "generate", "mos"],
    "deepseek": ["completion", "chat", "api/v"],
    "doubao": ["completion", "conversation", "chat", "message", "stream_call_bot", "samantha"],
}

# 需要脱敏的请求头（值用占位符替换，运行时再注入真实值）
SENSITIVE_HEADERS = {
    "authorization", "cookie", "x-authorization",
    "x-tt-passport-authorization", "x-device-id", "x-signature",
}

# 风控 / 人机验证的流量，永远不是对话接口，必须在录制时就排除。
# 踩过的坑：阿里 Baxia 的拦截路径是
#     https://chat2.qianwen.com/api/v2/chat/_____tmd_____/report
# 它就长在 chat 路径下面，URL_HINTS 里的 "chat" / "qianwen" 全部命中；而这条 report 的
# 请求 body 里是一整张 canvas 指纹（base64 WebP），体积远比对话响应大，于是 pick_endpoint
# 按 hint + size 打分时它把冠军抢走，写出的 fixture 拿去回放完全跑错方向。
# 不收裸 "verify"：有些站点的正常接口也带这个词，误伤代价比漏放代价大。
RISK_URL_MARKERS = (
    "_____tmd_____",
    "x5secdata",
    "slidedata",
    "/punish",
    "baxia",
    "nocaptcha",
    "secsdk",
    "captcha",
)


def is_risk_control(url: str) -> bool:
    """是否风控通道流量（滑块 / 处罚页 / 指纹上报）。"""
    low = (url or "").lower()
    return any(m in low for m in RISK_URL_MARKERS)


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: ("{{RUNTIME_INJECT:" + k + "}}" if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


def pick_endpoint(key: str, entries: list[dict]) -> Optional[dict]:
    if not entries:
        return None
    hints = URL_HINTS.get(key, [])

    def score(e: dict) -> tuple:
        ct = e.get("response_content_type", "")
        url = (e.get("url") or "").lower()
        is_stream = "event-stream" in ct or "stream" in ct
        hint_hit = any(h in url for h in hints)
        size = len(e.get("response_sample", ""))
        return (is_stream, e.get("method") == "POST" and hint_hit, hint_hit, size)

    return max(entries, key=score)


class NetworkRecorder:
    """attach 到一个 page 上，记录 xhr/fetch 请求；stop 后可写出 fixture。"""

    def __init__(self):
        self.entries: list[dict] = []
        self.skipped_risk = 0  # 被当作风控流量丢掉的条数
        self._page = None
        self._handler = None

    def attach(self, page) -> None:
        self._page = page
        self.entries = []
        self.skipped_risk = 0

        async def on_response(resp):
            try:
                req = resp.request
                if req.resource_type not in ("xhr", "fetch"):
                    return
                if is_risk_control(resp.url):
                    # 入库前就拦：candidates 也得保持干净，否则看录制结果会被误导
                    self.skipped_risk += 1
                    return
                if len(self.entries) >= MAX_ENTRIES:
                    return
                ct = (resp.headers or {}).get("content-type", "")
                sample = ""
                if "event-stream" in ct or "json" in ct:
                    try:
                        sample = (await resp.text())[:SAMPLE_CAP]
                    except Exception:  # noqa: BLE001 - 流式响应可能无法整体读取
                        sample = ""
                self.entries.append(
                    {
                        "url": resp.url,
                        "method": req.method,
                        "status": resp.status,
                        "request_headers": mask_headers(dict(req.headers or {})),
                        "post_data": req.post_data,
                        "response_content_type": ct,
                        "response_sample": sample,
                    }
                )
            except Exception:  # noqa: BLE001
                pass

        def _cb(resp):
            asyncio.ensure_future(on_response(resp))

        self._handler = _cb
        page.on("response", _cb)

    def detach(self) -> None:
        if self._page is not None and self._handler is not None:
            try:
                self._page.remove_listener("response", self._handler)
            except Exception:  # noqa: BLE001
                pass
        self._handler = None
        self._page = None

    def write_fixture(self, key: str) -> dict:
        chosen = pick_endpoint(key, self.entries)
        if chosen is None:
            # 未抓到任何候选请求：保留原有 fixture，绝不写空文件覆盖
            if self.skipped_risk:
                # 只抓到风控流量 = 页面当时整个被 punish 拦着，对话根本没发出去
                msg = (
                    f"只抓到 {self.skipped_risk} 条风控/滑块流量，没抓到对话请求："
                    "页面当时正被人机验证拦着。已保留原 fixture；"
                    "请先换网络过了验证、确认页面能正常回话后再重录"
                )
            else:
                msg = "未抓到候选请求，已保留原 fixture；请在页面里发一句对话后重录"
            logger.warning(
                f"[recon] {key}: 未抓到对话请求（已排除风控流量 {self.skipped_risk} 条），"
                "已保留原 fixture（未覆盖）"
            )
            return {"count": 0, "endpoint": "", "file": None, "skipped": True,
                    "risk_skipped": self.skipped_risk, "message": msg}

        body_template = None
        if chosen.get("post_data"):
            try:
                body_template = json.loads(chosen["post_data"])
            except json.JSONDecodeError:
                body_template = chosen["post_data"]

        out = {
            "provider": key,
            "captured_at": time.time(),
            "sample_prompt": RECON_SAMPLE_PROMPT,
            "endpoint": chosen["url"],
            "method": chosen["method"],
            "headers": chosen["request_headers"],
            "body_template": body_template,
            "response_content_type": chosen["response_content_type"],
            "response_sample": chosen["response_sample"],
            "candidates": [
                {"url": e["url"], "method": e["method"], "status": e["status"], "content_type": e["response_content_type"]}
                for e in self.entries
            ],
        }
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        dest = FIXTURES_DIR / f"{key}.json"
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            dest.chmod(0o600)  # fixture 可能含真实凭证，收紧权限
        except OSError:
            pass
        logger.info(f"[recon] {key}: 记录 {len(self.entries)} 个请求，端点={out['endpoint'] or '未识别'}")
        return {
            "count": len(self.entries),
            "endpoint": out["endpoint"],
            "file": str(dest.relative_to(BASE_DIR)),
            # 报出来才看得出“录到了但大半被拦”这种半成品，别让人拿着脏 fixture 去 replay
            "risk_skipped": self.skipped_risk,
        }
