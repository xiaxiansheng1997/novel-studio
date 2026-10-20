"""Provider 注册表：依 config 实例化适配器、按 model 前缀路由、聚合模型列表、持有各限流器。"""
from __future__ import annotations

from typing import Optional

from app.api.schemas import ModelInfo
from app.config import AppConfig
from app.core.base import AppContext, ProviderAdapter
from app.core.errors import ModelNotFound
from app.core.ratelimit import ProviderThrottle

# provider key -> 适配器类。新增站点在此登记。
def _adapter_classes() -> dict[str, type[ProviderAdapter]]:
    from app.providers.qwen import QwenAdapter
    from app.providers.doubao import DoubaoAdapter
    from app.providers.deepseek import DeepSeekAdapter

    return {"qwen": QwenAdapter, "doubao": DoubaoAdapter, "deepseek": DeepSeekAdapter}


class Registry:
    def __init__(self, config: AppConfig, ctx: AppContext):
        self.config = config
        self.ctx = ctx
        self.adapters: dict[str, ProviderAdapter] = {}
        self.throttles: dict[str, ProviderThrottle] = {}

        classes = _adapter_classes()
        for key, pcfg in config.providers.items():
            if not pcfg.enabled:
                continue
            cls = classes.get(key)
            if cls is None:
                continue
            self.adapters[key] = cls(pcfg, ctx)
            self.throttles[key] = ProviderThrottle(
                key,
                config.rate_limit_per_minute,
                pcfg.max_concurrency,
                pcfg.min_interval_sec,
            )

    def route(self, model: str) -> ProviderAdapter:
        for adapter in self.adapters.values():
            if adapter.matches(model):
                return adapter
        raise ModelNotFound(model)

    def throttle(self, key: str) -> ProviderThrottle:
        return self.throttles[key]

    def all_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        for adapter in self.adapters.values():
            models.extend(adapter.default_models())
        return models

    async def start_all(self) -> None:
        for adapter in self.adapters.values():
            await adapter.start()

    async def stop_all(self) -> None:
        for adapter in self.adapters.values():
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001 - 关停阶段吞掉单个错误
                pass

    def get(self, key: str) -> Optional[ProviderAdapter]:
        return self.adapters.get(key)
