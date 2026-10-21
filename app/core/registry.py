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

    def apply_config(self, config: AppConfig) -> dict[str, list[str]]:
        """就地把新配置应用到已运行的适配器上（供 /admin/reload）。

        适配器的 cfg 是引用替换：正在跑的流继续用旧配置，下一轮生效，不会被打断。
        限流参数（每分钟/并发/最小间隔）变化时重建 throttle，窗口计数会归零。
        返回新增/更新/移除的 provider，便于日志与接口回显。
        """
        self.config = config
        self.ctx.config = config
        classes = _adapter_classes()
        desired = {
            key: pcfg for key, pcfg in config.providers.items() if pcfg.enabled and key in classes
        }
        added: list[str] = []
        updated: list[str] = []
        removed: list[str] = []

        for key, pcfg in desired.items():
            adapter = self.adapters.get(key)
            if adapter is None:
                self.adapters[key] = classes[key](pcfg, self.ctx)
                added.append(key)
            else:
                adapter.cfg = pcfg
                updated.append(key)
            old = self.throttles.get(key)
            limit = config.rate_limit_per_minute
            if (
                old is None
                or old.per_minute != max(1, limit)
                or old.max_concurrency != max(1, pcfg.max_concurrency)
            ):
                self.throttles[key] = ProviderThrottle(
                    key, limit, pcfg.max_concurrency, pcfg.min_interval_sec
                )
            else:
                old.min_interval = max(0.0, float(pcfg.min_interval_sec))

        for key in list(self.adapters):
            if key not in desired:
                self.adapters.pop(key, None)
                self.throttles.pop(key, None)
                removed.append(key)

        return {"added": added, "updated": updated, "removed": removed}

    def all_models(self) -> list[ModelInfo]:
        """合并三家模型，并补上「模型名即模式」的别名，便于客户端发现。"""
        from app.core.modes import alias_ids

        models: list[ModelInfo] = []
        for adapter in self.adapters.values():
            models.extend(adapter.default_models())
            owner = adapter.cfg.display_name or adapter.key
            models.extend(ModelInfo(id=alias, owned_by=owner) for alias in alias_ids(adapter.cfg))
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
