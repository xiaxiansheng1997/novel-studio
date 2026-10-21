"""统一鉴权与来源校验：/v1、/admin 的访问口令，以及 /live、CORS 的来源白名单。

设计要点：
- `W2A_API_KEY` 为空表示不校验（本机自用的默认状态）；非空时 /v1 与 /admin 一律要求
  `Authorization: Bearer <key>`。比较用 `hmac.compare_digest`，不做逐字符提前返回。
- 来源白名单只用于**浏览器**发起的跨站请求（无 Origin 的 curl / 本地脚本仍放行），
  避免任意网页通过 DNS rebinding 或直接 fetch 驱动本机浏览器。
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import HTTPException, Request

# 前端 vite dev server 的默认地址：代理到 8000 时需要被放行
DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


def bearer_token(request: Request) -> Optional[str]:
    """取请求里的口令：支持 `Authorization: Bearer x` 与裸 token 两种写法。"""
    auth = request.headers.get("authorization", "").strip()
    if not auth:
        return None
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return auth


def expected_key(request: Request) -> Optional[str]:
    return getattr(request.app.state.settings, "api_key", None) or None


def key_matches(provided: Optional[str], expected: Optional[str]) -> bool:
    """口令校验：expected 为空表示不校验；否则必须常量时间相等。"""
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


async def verify_key(request: Request) -> None:
    """FastAPI 依赖：口令不对直接 401（OpenAI 客户端会按 invalid_api_key 处理）。"""
    if not key_matches(bearer_token(request), expected_key(request)):
        raise HTTPException(status_code=401, detail="无效的 API Key")


def allowed_origins(settings) -> set[str]:
    """允许的来源集合。`W2A_ALLOW_ORIGINS=*` 表示完全放开（仅建议在受信网络里用）。"""
    raw = (getattr(settings, "allow_origins", "") or "").strip()
    if raw == "*":
        return {"*"}
    hosts = {"127.0.0.1", "localhost"}
    host = getattr(settings, "host", "") or ""
    if host and host not in ("0.0.0.0", "::"):
        hosts.add(host)
    out = set(DEV_ORIGINS)
    for h in hosts:
        for scheme in ("http", "https"):
            out.add(f"{scheme}://{h}:{settings.port}")
    out.update(x.strip() for x in raw.split(",") if x.strip())
    return out


def origin_ok(origin: Optional[str], settings) -> bool:
    """无 Origin（curl、同源脚本）放行；有 Origin 时必须在白名单内。"""
    if origin is None:
        return True
    origins = allowed_origins(settings)
    return "*" in origins or origin in origins
