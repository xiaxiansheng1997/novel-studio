"""配置加载：.env（敏感）经 pydantic-settings，config.yaml（非敏感）经 YAML。

对外统一通过 get_settings() / get_config() 获取，均为进程内单例。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

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

    def resolved_storage_dir(self) -> Path:
        d = self.storage_dir
        if not d.is_absolute():
            d = BASE_DIR / d
        d.mkdir(parents=True, exist_ok=True)
        return d


class ProviderConfig(BaseModel):
    """单个 provider 的非敏感配置。"""

    key: str
    enabled: bool = True
    display_name: str = ""
    model_prefix: str = ""
    home_url: str = ""
    endpoint: str = ""
    headless: bool = False
    models: list[str] = Field(default_factory=list)
    max_concurrency: int = 1


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
