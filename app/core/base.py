"""Provider 适配器基类与运行时上下文。

设计要点：
- provider 只需实现 stream(req) -> AsyncIterator[DeltaEvent]，把各家真实流量归一成增量事件；
  流式与非流式都由上层复用该增量流（非流式用 sse.collect 聚合）。
- AppContext 汇总共享依赖（浏览器池、会话管理、存储、配置），注入给各适配器。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from app.api.schemas import ChatCompletionRequest, ModelInfo
from app.config import AppConfig, ProviderConfig
from app.core.sse import DeltaEvent


@dataclass
class AppContext:
    """共享运行时依赖，启动时装配并注入适配器。"""

    config: AppConfig
    # 延迟导入避免循环依赖
    sessions: "object"  # app.browser.session_manager.SessionManager
    store: "object"     # app.core.store.ConversationStore
    live: "object" = None  # app.browser.live_browser.LiveBrowserManager (optional)


class ProviderAdapter(abc.ABC):
    """各家网页端的统一适配接口。"""

    def __init__(self, cfg: ProviderConfig, ctx: AppContext):
        self.cfg = cfg
        self.ctx = ctx
        # 最近一次「把页面切到目标模式」的结果（供 /v1/capabilities 与管理页展示）
        self._last_mode_status: Optional[dict] = None
        # 本次请求解析出的模式计划（重发前需要再对齐一次）
        self._mode_plan: Optional[Any] = None

    # ------- 元信息 -------
    @property
    def key(self) -> str:
        return self.cfg.key

    def matches(self, model: str) -> bool:
        return model.startswith(self.cfg.model_prefix) or model == self.cfg.key

    def default_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=m.id, owned_by=self.cfg.display_name or self.key) for m in self.cfg.models]

    @property
    def last_mode_status(self) -> Optional[dict]:
        return self._last_mode_status

    def capability_payload(self) -> dict:
        """给 /v1/capabilities 与管理页用的能力描述（模型/模式/别名）。"""
        from app.core.modes import alias_ids, capability_features

        return {
            "key": self.key,
            "display_name": self.cfg.display_name or self.key,
            "models": [
                {"id": m.id, "label": m.label or m.id, "features": m.features}
                for m in self.cfg.models
            ],
            "aliases": alias_ids(self.cfg),
            "modes": [
                {"id": k, "label": v.label or k, "features": v.features}
                for k, v in self.cfg.modes.items()
            ],
            "features": capability_features(self.cfg),
            "last_mode_status": self._last_mode_status,
        }

    # ------- 生命周期 -------
    async def start(self) -> None:
        """可选：预热连接/浏览器。默认空实现。"""

    async def stop(self) -> None:
        """可选：释放资源。默认空实现（浏览器由 pool 统一管理）。"""

    # ------- 会话 -------
    @abc.abstractmethod
    async def ensure_session(self) -> None:
        """校验登录态；缺失/失效时抛 NeedsLogin / AuthExpired。"""

    # ------- 核心：对话增量流 -------
    @abc.abstractmethod
    def stream(self, req: ChatCompletionRequest) -> AsyncIterator[DeltaEvent]:
        """向各家发起一次对话，产出归一增量事件。内部自行处理限流/并发之外的会话细节。"""

    # ------- 多轮辅助 -------
    async def resolve_internal_id(self, req: ChatCompletionRequest) -> Optional[str]:
        """根据客户端 conversation_id 取出该 provider 的内部会话 id（若有）。"""
        if not req.conversation_id:
            return None
        return await self.ctx.store.get(req.conversation_id, self.key)

    async def save_internal_id(self, req: ChatCompletionRequest, internal_id: str) -> None:
        if req.conversation_id and internal_id:
            await self.ctx.store.set(req.conversation_id, self.key, internal_id)
