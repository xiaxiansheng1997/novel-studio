"""CDP 真流式捕获：Network.streamResourceContent + Network.dataReceived。

链路：
1. Network.enable
2. requestWillBeSent 记录 POST/PUT 候选
3. responseReceived 命中 SSE 后调用 Network.streamResourceContent
   - 返回的 bufferedData 是开启流式前已缓冲的字节（base64）
   - 之后 dataReceived 的 data 字段是增量（base64）
4. loadingFinished / loadingFailed 收尾
5. 拿不到任何数据时用 Network.getResponseBody 兜底

刻意不用 Fetch.takeResponseBodyAsStream：它会把响应体从页面夺走，
网页自身 UI 收不到回答，影响后续多轮。
"""
from __future__ import annotations

import asyncio
import base64
import codecs
from typing import Callable, Optional

from loguru import logger

from app.config import get_settings

# 与 stream_capture 共用的队列哨兵
DONE = "__W2A_DONE__"
ERR_PREFIX = "__W2A_ERR__"

# 出问题时设 W2A_CDP_STREAM=0 可一键退回整段读取
CDP_STREAM = get_settings().cdp_stream

_STREAM_TYPES = ("event-stream", "stream", "ndjson")


class CdpSseTee:
    """旁路读取页面 SSE 响应，不干扰页面自身的响应消费。"""

    def __init__(self, page, cdp) -> None:
        self.page = page
        self.cdp = cdp
        self._queue: Optional[asyncio.Queue] = None
        self._url_hint = ""
        self._armed = False
        self._on_fallback: Optional[Callable[[str], None]] = None
        self._req: Optional[dict] = None
        self._candidates: dict[str, dict] = {}
        self._tasks: set[asyncio.Task] = set()

        self.cdp.on("Network.requestWillBeSent", self._on_request)
        self.cdp.on("Network.responseReceived", self._on_response)
        self.cdp.on("Network.dataReceived", self._on_data)
        self.cdp.on("Network.loadingFinished", self._on_finished)
        self.cdp.on("Network.loadingFailed", self._on_failed)

    @classmethod
    async def for_page(cls, page) -> "CdpSseTee":
        """每个 capture 页面复用一个 CDP session，避免监听器累积。"""
        tee = getattr(page, "_w2a_cdp_tee", None)
        if tee is not None:
            return tee
        cdp = await page.context.new_cdp_session(page)
        try:
            await asyncio.wait_for(
                cdp.send(
                    "Network.enable",
                    {
                        # 放大缓冲，保证长回答下 getResponseBody 兜底仍可用
                        "maxTotalBufferSize": 100 * 1024 * 1024,
                        "maxResourceBufferSize": 50 * 1024 * 1024,
                        "maxPostDataSize": 64 * 1024,
                    },
                ),
                timeout=10,
            )
        except Exception:
            try:
                await cdp.detach()
            except Exception:  # noqa: BLE001
                pass
            raise
        tee = cls(page, cdp)
        page._w2a_cdp_tee = tee
        logger.info("CDP 真流式通道已就绪 (Network.streamResourceContent)")
        return tee

    # ---------- 生命周期 ----------
    def arm(self, queue: asyncio.Queue, url_hint: str, on_fallback=None) -> None:
        """每次对话前重置：只捕获一个流。"""
        self._queue = queue
        self._url_hint = url_hint or ""
        self._on_fallback = on_fallback
        self._armed = True
        self._req = None
        self._candidates.clear()

    def disarm(self) -> None:
        self._armed = False
        self._url_hint = ""
        self._queue = None
        self._on_fallback = None

    async def detach(self) -> None:
        try:
            await self.cdp.detach()
        except Exception:  # noqa: BLE001
            pass

    # ---------- CDP 事件 ----------
    def _on_request(self, ev: dict) -> None:
        if not self._armed:
            return
        req = ev.get("request") or {}
        if (req.get("method") or "").upper() not in ("POST", "PUT"):
            return
        self._candidates[ev.get("requestId", "")] = {"url": req.get("url", "")}

    def _on_response(self, ev: dict) -> None:
        if not self._armed:
            return
        rid = ev.get("requestId", "")
        cand = self._candidates.pop(rid, None)
        if not cand:
            return
        resp = ev.get("response") or {}
        url = resp.get("url") or cand.get("url", "")
        if self._url_hint and self._url_hint.lower() not in url.lower():
            return
        if int(resp.get("status") or 0) >= 400:
            return

        ctype = ""
        for k, v in (resp.get("headers") or {}).items():
            if str(k).lower() == "content-type":
                ctype = str(v).lower()
                break
        blob = (ctype or str(resp.get("mimeType") or "")).lower()
        if not any(t in blob for t in _STREAM_TYPES):
            logger.debug(f"CDP 忽略非流式响应: ct={ctype or resp.get('mimeType')} url={url[:90]}")
            return

        self._armed = False
        req = {
            "id": rid,
            "queue": self._queue,
            "url": url,
            "decoder": codecs.getincrementaldecoder("utf-8")("replace"),
            "emitted": 0,
            "recv_total": 0,
            "data_seen": False,
            "buffered_done": False,
            "finished": False,
            "done": False,
            "fallback_called": False,
            "open_task": None,
            "final_task": None,
        }
        self._req = req
        logger.info(f"CDP 流式接管: {url[:90]}")
        req["open_task"] = self._schedule(self._open_stream(req))

    def _on_data(self, ev: dict) -> None:
        req = self._req
        if not req or ev.get("requestId") != req["id"]:
            return
        if req["done"] or req["fallback_called"]:
            return
        length = int(ev.get("dataLength") or 0)
        data = ev.get("data")
        if not data:
            # 未开流式时只有长度；仍要推进偏移，避免后续块错位
            req["recv_total"] += length
            return
        self._feed_b64(req, data, length)

    def _on_finished(self, ev: dict) -> None:
        req = self._req
        if not req or ev.get("requestId") != req["id"]:
            return
        req["finished"] = True
        self._finalize(req)

    def _on_failed(self, ev: dict) -> None:
        req = self._req
        if not req or ev.get("requestId") != req["id"]:
            return
        if req["done"]:
            return
        error = ev.get("errorText") or ev.get("blockedReason") or "loadingFailed"
        req["done"] = True
        req["queue"].put_nowait(f"{ERR_PREFIX}CDP 流加载失败: {error}")
        req["queue"].put_nowait(DONE)
        logger.warning(f"CDP 流加载失败: {error}")

    # ---------- 读取 ----------
    async def _open_stream(self, req: dict) -> None:
        try:
            res = await asyncio.wait_for(
                self.cdp.send(
                    "Network.streamResourceContent", {"requestId": req["id"]}
                ),
                timeout=10,
            )
        except asyncio.TimeoutError:
            self._fallback(req, "streamResourceContent 超时")
            return
        except Exception as exc:  # noqa: BLE001
            self._fallback(req, f"streamResourceContent 失败: {exc}")
            return
        if req["done"]:
            return
        data = (res or {}).get("bufferedData") or ""
        if data:
            self._feed_b64(req, data, None)
        req["buffered_done"] = True
        if req["finished"]:
            self._finalize(req)

    def _feed_b64(self, req: dict, b64: str, data_length: Optional[int]) -> None:
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return
        if not raw:
            return

        if data_length is None:
            # bufferedData：包含 0..len(raw) 的完整历史
            if len(raw) > req["emitted"]:
                self._emit_bytes(req, raw[req["emitted"]:])
                req["emitted"] = len(raw)
            req["recv_total"] = max(req["recv_total"], len(raw))
            return

        # 增量块：用累计偏移裁剪，兼容与 bufferedData 的重叠
        start = req["recv_total"]
        req["recv_total"] += data_length
        end = start + len(raw)
        if end <= req["emitted"]:
            return
        piece = raw if start >= req["emitted"] else raw[req["emitted"] - start:]
        req["emitted"] = max(req["emitted"], end)
        self._emit_bytes(req, piece)

    def _emit_bytes(self, req: dict, raw: bytes) -> None:
        if not raw:
            return
        text = req["decoder"].decode(raw)
        if text:
            req["data_seen"] = True
            req["queue"].put_nowait(text)

    def _finalize(self, req: dict) -> None:
        if req["done"] or req["final_task"] or not req["buffered_done"]:
            return
        req["final_task"] = self._schedule(self._finish(req))

    async def _finish(self, req: dict) -> None:
        if req["fallback_called"]:
            return
        tail = req["decoder"].decode(b"", final=True)
        if tail:
            req["data_seen"] = True
            req["queue"].put_nowait(tail)
        if not req["data_seen"] and req["emitted"] == 0:
            body = await self._get_body(req["id"])
            if body:
                req["queue"].put_nowait(body)
                req["data_seen"] = True
            else:
                self._fallback(req, "未取到任何响应数据")
                return
        req["done"] = True
        req["queue"].put_nowait(DONE)

    async def _get_body(self, rid: str) -> str:
        try:
            res = await asyncio.wait_for(
                self.cdp.send("Network.getResponseBody", {"requestId": rid}), timeout=5
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"getResponseBody 兜底失败: {exc}")
            return ""
        body = (res or {}).get("body") or ""
        if not body:
            return ""
        if res.get("base64Encoded"):
            try:
                return base64.b64decode(body).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return ""
        return body

    def _fallback(self, req: dict, reason: str) -> None:
        if req["fallback_called"]:
            return
        req["fallback_called"] = True
        req["done"] = True
        # 短响应可能在 streamResourceContent 到达前就结束，属正常回落，不刷告警
        benign = "already finished loading" in reason
        (logger.debug if benign else logger.warning)(f"CDP 流式未接管，改走整段读取: {reason}")
        if self._on_fallback is not None:
            self._on_fallback(reason)
            return
        req["queue"].put_nowait(f"{ERR_PREFIX}CDP 流式捕获失败: {reason}")
        req["queue"].put_nowait(DONE)

    def _schedule(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
