"""配置加载：.env（敏感）经 pydantic-settings，config.yaml（非敏感）经 YAML。

对外统一通过 get_settings() / get_config() 获取，均为进程内单例。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录 = app/ 的上一级
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """来自环境变量 / .env 的运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="W2A_",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8000
    # 可选访问口令；None/空串表示不校验
    api_key: Optional[str] = None
    storage_dir: Path = Path("storage")
    # CDP 真流式：CDP 不可用或异常时可在 .env 设 0，退回整段读取
    cdp_stream: bool = True
    # 流式空闲超时（秒）：超过该时间没有新数据则结束本次读取
    stream_idle_timeout: float = 60.0

    def resolved_storage_dir(self) -> Path:
        d = self.storage_dir
        if not d.is_absolute():
            d = BASE_DIR / d
        d.mkdir(parents=True, exist_ok=True)
        return d


class ModelDef(BaseModel):
    """对外的模型 id 与它在网页端下拉里的显示文案。

    label 为空表示该 provider 没有模型下拉（如 DeepSeek），不驱动模型切换。
    """

    id: str
    label: str = ""
    # 这个模型在网页端默认带哪些特性（如 deepseek-reasoner 默认开思考）
    features: dict[str, bool] = Field(default_factory=dict)


class UiControlConfig(BaseModel):
    """网页端某个开关/下拉的定位文案与状态判定规则。"""

    # 按钮上可能出现的文本（含匹配，任一命中即可）
    labels: list[str] = Field(default_factory=list)
    # 下拉菜单里可点的项（精确匹配）
    item_labels: list[str] = Field(default_factory=list)
    # 类名命中这个正则即视为“已开启”（页面上没有 aria 状态时的兜底）
    active_class: str = ""
    # 状态完全读不出来时是否仍点一次（默认不点，避免把开着的点成关）
    blind: bool = False


class ModeSpecConfig(BaseModel):
    """一个模式预设：要开哪些开关，以及 HTTP 快路径要改哪些 body 字段。"""

    label: str = ""
    features: dict[str, bool] = Field(default_factory=dict)
    # 该模式要求的网页端模型下拉文案（空则沿用模型自带的 label）
    model_label: str = ""
    # 仅 HTTP 快路径使用：直接覆盖录制出来的 body 字段
    body_patch: dict[str, Any] = Field(default_factory=dict)


class ProviderConfig(BaseModel):
    """单个 provider 的非敏感配置。"""

    key: str
    enabled: bool = True
    display_name: str = ""
    model_prefix: str = ""
    home_url: str = ""
    endpoint: str = ""
    headless: bool = False
    models: list[ModelDef] = Field(default_factory=list)
    # 模式预设：quick / think / search / expert，键名即对外 mode 参数
    modes: dict[str, ModeSpecConfig] = Field(default_factory=dict)
    # 开关定位：thinking / search / model
    ui: dict[str, UiControlConfig] = Field(default_factory=dict)
    max_concurrency: int = 1
    # 相邻两次请求的最小间隔（秒，从上一次回复结束算起）；0 表示不限制
    min_interval_sec: float = 0.0

    def model_ids(self) -> list[str]:
        return [m.id for m in self.models]


class AppConfig(BaseModel):
    """config.yaml 的结构化映射。"""

    title: str = "Web2API 对话反代"
    log_level: str = "INFO"
    rate_limit_per_minute: int = 20
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        providers: dict[str, ProviderConfig] = {}
        for key, val in (raw.get("providers") or {}).items():
            val = dict(val or {})
            val.setdefault("display_name", key)
            val["key"] = key
            # models 兼容两种写法：["qwen-plus"] 与 [{id: qwen-plus, label: Qwen3.5-Plus}]
            val["models"] = [
                m if isinstance(m, dict) else {"id": str(m)} for m in (val.get("models") or [])
            ]
            providers[key] = ProviderConfig(**val)
        return cls(
            title=(raw.get("server") or {}).get("title", cls.model_fields["title"].default),
            log_level=(raw.get("logging") or {}).get("level", "INFO"),
            rate_limit_per_minute=raw.get("rate_limit_per_minute", 20),
            providers=providers,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_config() -> AppConfig:
    cfg_path = BASE_DIR / "config.yaml"
    if not cfg_path.exists():
        return AppConfig()
    return AppConfig.load(cfg_path)
