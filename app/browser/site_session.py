"""站点会话隔离：把「API 会话」映射到「站点会话 id」，并在切换时导航/新开。

背景：网页端标签页只有一个，站点自己维护对话上下文。若不同 API 会话复用同一个
标签页，上下文会串台（A 的追问会被 B 接着答）。这里做两件事：

1. `extract_chat_id`：从页面 URL 提取站点会话 id（各站点路径形态不同，取末段）；
2. `open_conversation`：切到目标站点会话——有直链模板（config 的 `chat_url`，
   例如 DeepSeek 的 `https://chat.deepseek.com/a/chat/s/{id}`）就导航，
   没有模板就点「新对话」/回首页，宁可丢上下文也不串上下文。

站点改版导致提取不准时，只影响复用（会退化为新开会话），不会把请求卡死。
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from loguru import logger

# 会话 id 常见形态：uuid、32/64 位 hex、字母数字混排（不含空白/斜杠）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,72}$")
# 这些末段明显不是会话 id（首页/接口/静态资源/固定路径段）
_NOT_ID = {
    "index", "home", "chat", "new", "login", "signin", "signup", "api",
    "completion", "conversation", "settings", "setting", "profile", "welcome",
    "discover", "explore", "about", "pricing", "download",
}
# 各站点「新对话」按钮文案（顺序即优先级）
NEW_CHAT_LABELS = ["新对话", "新建对话", "开启新对话", "新建聊天", "New chat", "New Chat"]


def extract_chat_id(url: str) -> Optional[str]:
    """从站点 URL 的 path 末段提取会话 id；不像会话页就返回 None。"""
    try:
        path = urlparse(url or "").path.rstrip("/")
    except ValueError:
        return None
    if not path:
        return None
    seg = path.rsplit("/", 1)[-1]
    if seg.lower() in _NOT_ID or not _ID_RE.match(seg):
        return None
    return seg


async def _wait_input(page, timeout: int = 15000) -> None:
    """等新页面的输入框就绪；超时只记 debug（后面 trigger_send 会给出明确错误）。"""
    try:
        await page.wait_for_selector(
            "div[contenteditable='true'], textarea, [role='textbox']", timeout=timeout
        )
    except Exception:  # noqa: BLE001
        logger.debug("会话切换后等输入框超时（站点可能还在加载）")


async def _click_new_chat(page, labels: list[str]) -> bool:
    """按文案点「新对话」：优先 button/link 角色，避免点到正文里的同名文字。"""
    for label in labels:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=label)
                if await loc.count():
                    await loc.first.click(timeout=3000)
                    return True
            except Exception:  # noqa: BLE001 - 单个选择器失败就换下一个
                continue
        try:
            loc = page.get_by_text(label, exact=True)
            if await loc.count():
                await loc.first.click(timeout=3000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def open_conversation(page, cfg: Any, internal_id: Optional[str]) -> dict:
    """让页面切到 `internal_id` 对应的站点会话；没有直链就新开一个。

    返回 {"action": "reuse|navigate|new|home|none", "url": ..., "ok": bool, "error": str}
    """
    key = getattr(cfg, "key", "") or ""
    current = extract_chat_id(page.url)
    if internal_id and current == internal_id:
        return {"action": "reuse", "url": page.url, "ok": True, "error": ""}

    tpl = (getattr(cfg, "chat_url", "") or "").strip()
    if internal_id and tpl:
        target = tpl.replace("{id}", internal_id)
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            await _wait_input(page)
            logger.info(f"[{key}] 已导航到站点会话 {internal_id}: {page.url[:100]}")
            return {"action": "navigate", "url": page.url, "ok": True, "error": ""}
        except Exception as exc:  # noqa: BLE001 - 直链失效不能卡死请求，退化为新会话
            logger.warning(f"[{key}] 导航到站点会话失败（改为新开会话）: {exc}")
            err = str(exc)
    else:
        err = ""

    labels = list(getattr(cfg, "new_chat_labels", None) or NEW_CHAT_LABELS)
    try:
        if await _click_new_chat(page, labels):
            await _wait_input(page)
            logger.info(f"[{key}] 已点「新对话」开新站点会话: {page.url[:100]}")
            return {"action": "new", "url": page.url, "ok": True, "error": err}
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[{key}] 点新对话失败: {exc}")

    home = (getattr(cfg, "home_url", "") or "").strip()
    if home:
        try:
            await page.goto(home, wait_until="domcontentloaded", timeout=30000)
            await _wait_input(page)
            logger.info(f"[{key}] 已回首页开新站点会话: {page.url[:100]}")
            return {"action": "home", "url": page.url, "ok": True, "error": err}
        except Exception as exc:  # noqa: BLE001
            return {"action": "none", "url": page.url, "ok": False, "error": f"{err} | {exc}".strip(" |")}

    return {"action": "none", "url": page.url, "ok": False, "error": err or "找不到新会话入口"}
