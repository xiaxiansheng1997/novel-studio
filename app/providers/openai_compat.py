"""OpenAI 兼容直连通道：官方 API 与 Ollama 等本地服务，一条 httpx 打出去。

迭代 C 的定位：**反代降为 fallback**。批量生成优先走这里——没有风控、没有
滑块、不用养浏览器；官方 API 花的是钱，Ollama 花的是本地显卡，都比花账号
便宜。网页端反代（qwen/doubao/deepseek）依然全量可用，只是退居二线。

约定（见 app.core.base）：只实现 `stream(req)` 产出 DeltaEvent，流式与非流
式都由上层聚合。SSE 行缓冲复用 `_replay.SSEBuffer`：反代各站解析的是自家
协议，这里解析的是最标准的 OpenAI chunk，能复用就复用，坑只修一处。

鉴权 key 绝不进 config.yaml：条目里只写 `api_key_env`（环境变量名），
值从 .env 读。Ollama 不需要 key，留空即不带 Authorization 头。
"""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx

from app.api.schemas import ChatCompletionRequest
from app.core.base import ProviderAdapter
from app.core.errors import ProviderError
from app.core.sse import DeltaEvent
from app.providers._replay import SSEBuffer, is_done

# 首包可以很慢（Ollama 冷加载模型、官方排队），但字出来之后不该断太久
_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0)


class OpenAICompatAdapter(ProviderAdapter):
    """POST {base_url}/chat/completions（stream）→ DeltaEvent。"""

    def _make_client(self) -> httpx.AsyncClient:
        # 独立出来给测试留的口子：MockTransport 换掉它就完全不碰网
        return httpx.AsyncClient(timeout=_TIMEOUT)

    def _api_key(self) -> str:
        # 库里的配置（控制台页面填的，经 provider_compat 注入 cfg.api_key）优先，
        # 环境变量兜底：页面方案不该被旧 .env 里的值悄悄盖住
        return (self.cfg.api_key or os.environ.get(self.cfg.api_key_env or "", "")).strip()

    def upstream_model(self, model: str) -> str:
        """请求体里发给上游的模型名：ollama-qwen2.5:14b → qwen2.5:14b。"""
        prefix = f"{self.key}-"
        if self.cfg.strip_prefix and model.startswith(prefix):
            return model[len(prefix):]
        return model

    async def ensure_session(self) -> None:
        """没有登录态概念；只有「声明了要 key 却拿不到值」算没就绪。"""
        if self.cfg.api_key_env and not self._api_key():
            raise ProviderError(
                self.key,
                f"没有配 API key：控制台→状态→这张卡的「配置」里填（存库不进 git）；"
                f"也可以在 .env 设 {self.cfg.api_key_env}=sk-... 后重启服务",
                status=401,
            )

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def ready(self) -> bool:
        """「配了且现在能用吗」：config 里 enabled 只是作者 declaration，
        Ollama 没起、OPENAI_API_KEY 没设都算没就绪（审查 M2）。
        pick_model 拿它筛候选——就绪校验只能查「key 取得到」，
        查不了「服务活着没」：那要发网络请求，不该出现在选模型里。"""
        if not (self.cfg.base_url or "").strip():
            return False
        if self.cfg.api_key_env and not self._api_key():
            return False
        return bool(self.cfg.models)

    def _parse_chunk(self, payload: str) -> Optional[DeltaEvent]:
        """一条 OpenAI chunk → DeltaEvent；解不动的行（心跳/注释）返回 None。"""
        try:
            obj: Any = json.loads(payload)
        except ValueError:
            return None
        # 合法 JSON 但不是对象（null / 数字 / 裸数组）：跳行而不是抛 AttributeError
        # 穿透流式协程（审查 m6）
        if not isinstance(obj, dict):
            return None
        if obj.get("error"):
            err = obj["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            # 用 self.key 而不是硬编码类名：openai 与 ollama 的上游报错要在
            # 日志和响应里分得清是谁家的（审查 m8）
            raise ProviderError(self.key, f"上游报错：{detail}")
        choices = obj.get("choices") or []
        if not choices:
            return None
        c0 = choices[0] or {}
        delta = c0.get("delta") or {}
        ev = DeltaEvent(
            content=delta.get("content") or None,
            reasoning_content=delta.get("reasoning_content") or None,
            finish_reason=c0.get("finish_reason") or None,
        )
        if ev.content or ev.reasoning_content or ev.finish_reason:
            return ev
        return None

    async def stream(self, req: ChatCompletionRequest) -> AsyncIterator[DeltaEvent]:
        base = (self.cfg.base_url or "").rstrip("/")
        if not base:
            raise ProviderError(self.key, "没配 base_url（兼容端点根地址，如 …/v1）")
        if self.cfg.api_key_env and not self._api_key():
            await self.ensure_session()
        body = {
            "model": self.upstream_model(req.model),
            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
            "stream": True,
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        url = f"{base}/chat/completions"
        async with self._make_client() as client:
            try:
                async with client.stream("POST", url, headers=self._headers(), json=body) as resp:
                    if resp.status_code != 200:
                        raw = (await resp.aread()).decode("utf-8", "replace")[:300]
                        raise ProviderError(
                            self.key, f"上游 HTTP {resp.status_code}：{raw}",
                            status=502,
                        )
                    buf = SSEBuffer()
                    # 终止权不全交给 [DONE]：上游带着长连接不写它时，
                    # read=300 会把一次调用整整堵 5 分钟（审查 m7）。
                    # finish_reason 到手 = 模型已经说完，[DONE] 到手 = 上游说完了，
                    # 两个都算数。
                    done = False
                    async for text in resp.aiter_text():
                        for payload in buf.feed(text):
                            if not payload:
                                continue
                            if is_done(payload):
                                done = True
                                break
                            ev = self._parse_chunk(payload)
                            if ev is None:
                                continue
                            yield ev
                            if ev.finish_reason:
                                done = True
                                break
                        if done:
                            break
                    if not done:
                        # 流自然断开且没见终止信号：把最后没成行的一段补发掉
                        for payload in buf.flush():
                            if not payload or is_done(payload):
                                continue
                            ev = self._parse_chunk(payload)
                            if ev is not None:
                                yield ev
            except httpx.HTTPError as exc:
                raise ProviderError(self.key, f"连不上 {base}：{exc}") from exc
