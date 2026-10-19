"""会话/凭证管理：持久化 profile 复用、票据缓存、登录探测、状态汇总。

- 凭证缓存：storage/session/<key>.json（如通义的 ticket），文件权限收紧、随 storage 一起 gitignore。
- 登录探测：各家登录态载体不同（cookie / localStorage），用 sentinel 判定是否已登录。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from loguru import logger

from app.browser.playwright_pool import BrowserPool
from app.config import AppConfig

# 各家用于判定"已登录"的关键 cookie 名（命中其一即视为登录成功）
_COOKIE_SENTINELS: dict[str, list[str]] = {
    "qwen": ["login_tongyi_ticket", "tongyi_ticket", "login_aliyunid_ticket"],
    "doubao": ["sessionid", "passport_sessionid"],
    "deepseek": ["auth", "token"],
}

# 部分站点登录态存放在 localStorage（cookie 探测不到时兜底）
_LOCALSTORAGE_CHECKS: dict[str, str] = {
    "deepseek": "localStorage.getItem('auth') || localStorage.getItem('token')",
}


class SessionManager:
    def __init__(self, pool: BrowserPool, storage_dir: Path, config: AppConfig):
        self.pool = pool
        self.config = config
        self._cred_dir = storage_dir / "session"
        self._cred_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 凭证缓存 ----------
    def _cred_path(self, key: str) -> Path:
        return self._cred_dir / f"{key}.json"

    def load_cred(self, key: str) -> Optional[dict[str, Any]]:
        p = self._cred_path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def save_cred(self, key: str, data: dict[str, Any]) -> None:
        p = self._cred_path(key)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass  # Windows 上 chmod 语义有限，尽力而为

    # ---------- cookie / localStorage 读取 ----------
    async def find_cookie(self, key: str, names: Optional[list[str]] = None) -> Optional[tuple[str, str]]:
        """从该 provider 的持久化 context 中查找命中的 cookie，返回 (name, value)。"""
        names = names or _COOKIE_SENTINELS.get(key, [])
        ctx = await self.pool.context(key)
        cookies = await ctx.cookies()
        by_name = {c["name"]: c["value"] for c in cookies}
        for n in names:
            if by_name.get(n):
                return n, by_name[n]
        return None

    def _pick_site_page(self, key: str, ctx):
        """选一个确实停在目标站点上的页面（避开 about:blank 启动页）。"""
        pcfg = self.config.providers.get(key)
        host = urlparse(pcfg.home_url).netloc if pcfg and pcfg.home_url else ""
        pages = [p for p in ctx.pages if not p.is_closed()]
        if host:
            for p in pages:
                if host in (urlparse(p.url).netloc or ""):
                    return p
        for p in pages:
            if (p.url or "").startswith(("http://", "https://")):
                return p
        return None

    async def read_localstorage(self, key: str, js_expr: str) -> Optional[str]:
        """在该 provider 停在站点上的页面里读 localStorage（js_expr 返回字符串）。"""
        ctx = await self.pool.context(key)
        page = self._pick_site_page(key, ctx)
        if page is None:
            return None
        try:
            val = await page.evaluate(f"() => {{ try {{ return {js_expr}; }} catch(e) {{ return null; }} }}")
            return val if isinstance(val, str) and val else None
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"read_localstorage 失败 {key}: {exc}")
            return None

    # ---------- 登录探测 ----------
    async def wait_for_login(self, key: str, home_url: str, timeout: int = 240) -> Optional[tuple[str, str]]:
        """打开站点首页，轮询 sentinel cookie 判定登录完成；profile 落盘后长期复用。"""
        ctx = await self.pool.context(key, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if home_url and not page.url.startswith("http"):
            await page.goto(home_url, wait_until="domcontentloaded")

        logger.info(f"请在弹出的浏览器中完成 {key} 登录（扫码/密码/验证码），最长等待 {timeout}s ...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            hit = await self.find_cookie(key)
            if hit:
                logger.info(f"检测到登录态: cookie={hit[0]}")
                self.save_cred(key, {"updated_at": time.time(), "cookie_name": hit[0]})
                return hit
            await asyncio.sleep(3)
        logger.warning(f"未在时限内检测到 {key} 登录态（profile 已保留，可稍后重试或直接使用）")
        return None

    # ---------- 状态汇总 ----------
    async def is_logged_in(self, key: str) -> dict[str, Any]:
        """在**已打开**的 context 上探测登录态（不会主动启动浏览器）。

        先查 sentinel cookie，再兜底查 localStorage（如 DeepSeek 的 auth）。
        """
        if not self.pool.is_open(key):
            return {"logged_in": False, "browser_open": False}
        hit = await self.find_cookie(key)
        if hit:
            return {"logged_in": True, "via": "cookie", "name": hit[0], "browser_open": True}
        expr = _LOCALSTORAGE_CHECKS.get(key)
        if expr:
            val = await self.read_localstorage(key, expr)
            if val:
                return {"logged_in": True, "via": "localstorage", "browser_open": True}
        return {"logged_in": False, "browser_open": True}

    def has_profile(self, key: str) -> bool:
        d = self.pool.profile_dir(key)
        return d.exists() and any(d.iterdir())

    def status_all(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for k, pcfg in self.config.providers.items():
            cred = self.load_cred(k)
            out.append(
                {
                    "provider": k,
                    "display_name": pcfg.display_name,
                    "enabled": pcfg.enabled,
                    "has_profile": self.has_profile(k),
                    "browser_open": self.pool.is_open(k),
                    "cached_at": cred.get("updated_at") if cred else None,
                }
            )
        return out
