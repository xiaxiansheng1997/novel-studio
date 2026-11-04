"""直连通道（compat provider）的配置落库：base_url / API key / 模型清单 / 启停。

为什么不写 config.yaml：这个仓库要推 GitHub，带 key 的文件就是漏 key 的通道
——.env 虽然被 gitignore，但「把 key 写进 yaml 顺手提交」这种事迟早发生一次。
storage/ 整目录在 .gitignore 里，key 落进 sessions.sqlite 天然不进版本库；
桌面形态也不用用户翻文件改环境变量，控制台上填完即生效。

为什么明文存库不加密：本机单机工具，加密密钥也只能存在同一台机器上，
脱裤子放屁的加密是伪安全。真正的防线是所有读接口只回掩码
（admin_routes 的 _compat_row），明文 key 永远不出后端。

读写都用同步 sqlite3：get_config() 是同步 lru_cache（build_runtime 在启动
阶段就要读覆盖层），写入方是低频的 admin 操作。短事务 + 2 秒 busy_timeout
与 aiosqlite 共存同一个文件没有问题（与 studio 那边「一切皆异步」不同，
这里等不起一个异步装配链）。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# 与 ConversationStore._SCHEMA 共用同一段 DDL：两边都可能先于对方建表
PROVIDER_COMPAT_DDL = """
CREATE TABLE IF NOT EXISTS provider_compat (
    key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    models TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
"""


def db_path(storage_dir: Path) -> Path:
    return Path(storage_dir) / "sessions.sqlite"


def _connect(storage_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path(storage_dir)), timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_override(row: sqlite3.Row) -> dict[str, Any]:
    try:
        models = json.loads(row["models"] or "[]")
    except ValueError:
        models = []
    return {
        "key": row["key"],
        "display_name": row["display_name"],
        "base_url": row["base_url"],
        "api_key": row["api_key"],
        # 只收字符串项：手改库塞进脏东西时退回空清单，而不是把 dict 喂进 ModelDef
        "models": [str(m) for m in models if isinstance(m, (str, int, float))],
        "enabled": bool(row["enabled"]),
        "updated_at": row["updated_at"],
    }


def load_overrides(storage_dir: Path) -> dict[str, dict[str, Any]]:
    """全部 DB 覆盖，按 key 索引。库或表还没建出来 = 没有任何覆盖。"""
    if not db_path(storage_dir).exists():
        return {}
    try:
        with _connect(storage_dir) as conn:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_compat'"
            ).fetchone()
            if not has_table:
                return {}
            rows = conn.execute(
                "SELECT * FROM provider_compat ORDER BY key"
            ).fetchall()
    except sqlite3.Error:
        # 配置读取不该把整个服务拖垮：读不动就当没配过，界面保存时会再试
        return {}
    return {r["key"]: _row_to_override(r) for r in rows}


def get_row(storage_dir: Path, key: str) -> Optional[dict[str, Any]]:
    if not db_path(storage_dir).exists():
        return None
    try:
        with _connect(storage_dir) as conn:
            conn.executescript(PROVIDER_COMPAT_DDL)
            row = conn.execute(
                "SELECT * FROM provider_compat WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return _row_to_override(row) if row else None


def upsert_row(
    storage_dir: Path,
    *,
    key: str,
    display_name: str,
    base_url: str,
    api_key: str,
    models: list[str],
    enabled: bool,
) -> dict[str, Any]:
    """整行写入（调用方负责先读旧行做局部合并）。返回与 get_row 同形。"""
    with _connect(storage_dir) as conn:
        conn.executescript(PROVIDER_COMPAT_DDL)
        conn.execute(
            "INSERT INTO provider_compat(key, display_name, base_url, api_key, "
            "models, enabled, updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "display_name=excluded.display_name, base_url=excluded.base_url, "
            "api_key=excluded.api_key, models=excluded.models, "
            "enabled=excluded.enabled, updated_at=excluded.updated_at",
            (
                key,
                display_name.strip(),
                base_url.strip(),
                api_key.strip(),
                json.dumps([m.strip() for m in models if m.strip()], ensure_ascii=False),
                1 if enabled else 0,
                time.time(),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM provider_compat WHERE key = ?", (key,)).fetchone()
    return _row_to_override(row)


def delete_row(storage_dir: Path, key: str) -> bool:
    """删 DB 行；yaml 出厂条目删不掉（只可停用），删行后条目回退 config.yaml。"""
    if not db_path(storage_dir).exists():
        return False
    with _connect(storage_dir) as conn:
        conn.executescript(PROVIDER_COMPAT_DDL)
        cur = conn.execute("DELETE FROM provider_compat WHERE key = ?", (key,))
        conn.commit()
    return cur.rowcount > 0


def merge_into_raw(raw: dict, overrides: dict[str, dict[str, Any]]) -> dict:
    """把 DB 覆盖合进 config.yaml 的原始 dict（在 from_raw 之前）。

    规则（与 ui-bindings 覆盖层同一套路）：
    - yaml 有且是 compat 条目 → 逐字段覆盖（display_name 留空则沿用 yaml 的）；
    - yaml 没有 → 合成一条 compat 条目（model_prefix 取「{key}-」，
      strip_prefix=false：新建条目的模型 id 是什么就发什么，不猜别家约定）；
    - yaml 有但不是 compat → 跳过。防手改库把 qwen 这种反代站点改成直连，
      三家配置就再也回不来了。
    """
    if not overrides:
        return raw
    providers = raw.setdefault("providers", {})
    for key, ov in overrides.items():
        entry = providers.get(key)
        if entry is not None and not (entry or {}).get("compat"):
            continue
        model_ids = [{"id": m} for m in ov["models"]]
        if entry is None:
            providers[key] = {
                "compat": True,
                "display_name": ov["display_name"] or key,
                "model_prefix": f"{key}-",
                "strip_prefix": False,
                "base_url": ov["base_url"],
                "api_key": ov["api_key"],
                "enabled": ov["enabled"],
                "models": model_ids,
            }
            continue
        merged = dict(entry or {})
        merged["base_url"] = ov["base_url"]
        merged["api_key"] = ov["api_key"]
        merged["enabled"] = ov["enabled"]
        merged["models"] = model_ids
        if ov["display_name"]:
            merged["display_name"] = ov["display_name"]
        providers[key] = merged
    return raw
