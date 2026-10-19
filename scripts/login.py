"""一次性登录：为某个 provider 打开有头浏览器，人工完成登录，profile 落盘长期复用。

用法:  python -m scripts.login <qwen|doubao|deepseek>

说明:
- 用的是持久化 profile（storage/browser-profiles/<key>），登录一次后，服务端与 recon 都会复用。
- 通义会把 login_tongyi_ticket 缓存到 storage/session/qwen.json 供 HTTP 快路径使用。
"""
from __future__ import annotations

import asyncio
import sys
import time

from app.runtime import build_runtime, setup, teardown

# provider -> 登录后需要额外缓存的 localStorage 表达式（DeepSeek 把 token 放 localStorage）
_LOCALSTORAGE_CAPTURE: dict[str, str] = {
    "deepseek": "localStorage.getItem('auth')",
}


async def _do_login(rt, key: str) -> int:
    pcfg = rt.config.providers.get(key)
    if pcfg is None:
        print(f"未知 provider: {key}")
        print(f"可选: {', '.join(rt.config.providers.keys())}")
        return 2

    ctx = await rt.pool.context(key, headless=False)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.goto(pcfg.home_url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        print(f"打开首页失败（可手动在浏览器地址栏访问）: {exc}")

    print("\n================ 登录引导 ================")
    print(f"1) 在弹出的浏览器中完成【{pcfg.display_name}】登录（扫码 / 手机号 / 邮箱 / 人机验证）")
    print("2) 确认已能正常打开对话页面")
    print("3) 回到本终端，按【回车】结束（profile 会自动保存）")
    print("=========================================\n")
    await asyncio.to_thread(input, "登录完成后按回车继续 ... ")

    cached: dict[str, object] = {"updated_at": time.time()}
    hit = await rt.sessions.find_cookie(key)
    if hit:
        cached["cookie_name"] = hit[0]
        cached["cookie_value"] = hit[1]
        print(f"已缓存关键 cookie: {hit[0]}")

    ls_expr = _LOCALSTORAGE_CAPTURE.get(key)
    if ls_expr:
        val = await rt.sessions.read_localstorage(key, ls_expr)
        if val:
            cached["localstorage"] = val
            print("已缓存 localStorage 凭证")

    if any(k for k in cached if k != "updated_at"):
        rt.sessions.save_cred(key, cached)
        print(f"凭证已写入 storage/session/{key}.json（已 gitignore，勿外泄）")
    else:
        print("未探测到关键凭证；浏览器 profile 已保存，Playwright 路径仍会复用登录态。")
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        rt = build_runtime()
        keys = ", ".join(rt.config.providers.keys())
        print(f"用法: python -m scripts.login <{keys}>")
        return
    key = sys.argv[1]

    async def _run():
        rt = build_runtime()
        await setup(rt)
        try:
            return await _do_login(rt, key)
        finally:
            await teardown(rt)

    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
