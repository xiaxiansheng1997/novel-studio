"""给写作台截图，用来肉眼验收 UI（不参与业务，只做验证）。

用法：
    .venv\\Scripts\\python.exe -m scripts.shot_studio
    .venv\\Scripts\\python.exe -m scripts.shot_studio --path /studio/<work_id>

产物：storage/shots/*.png
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
SHOT_DIR = BASE_DIR / "storage" / "shots"


def load_env() -> None:
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--path", default="/studio")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--out", default="")
    ap.add_argument("--generate", action="store_true", help="点一次「生成」并等预览出来")
    ap.add_argument("--click", default="", help="截图前依次点击，多个用 || 分隔（支持 text=）")
    ap.add_argument("--timeout", type=int, default=180000)
    args = ap.parse_args()

    load_env()
    key = os.environ.get("W2A_API_KEY", "")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else SHOT_DIR / (args.path.strip("/").replace("/", "_") or "root")
    out = out if out.suffix == ".png" else out.with_suffix(".png")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=1,
        )
        # 页面加载前先把访问口令塞进 localStorage，否则首屏全是 401
        await ctx.add_init_script(
            f"try{{localStorage.setItem('w2a_apikey', {key!r});"
            "localStorage.setItem('w2a_theme','dark');}catch(e){}"
        )
        page = await ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        # 不能用 networkidle：页面里有常驻连接，永远等不到空闲
        await page.goto(args.base_url + args.path, wait_until="domcontentloaded")
        await page.wait_for_selector(".app-shell, .rail", timeout=15000)
        await page.wait_for_timeout(1200)

        if args.click:
            for target in [x.strip() for x in args.click.split("||") if x.strip()]:
                loc = page.get_by_text(target[5:]) if target.startswith("text=") else page.locator(target)
                await loc.first.click()
                await page.wait_for_timeout(600)

        if args.generate:
            btn = page.locator(".gen-bar button").last
            await btn.click()
            # 生成中会出现预览区；等「采用」按钮出现即认为流式跑完
            await page.wait_for_selector(".gen-actions", timeout=args.timeout)
            await page.wait_for_timeout(400)
            gen_len = await page.locator(".gen-text").inner_text()
            print(f"generated={len(gen_len)} chars")
        await page.screenshot(path=str(out), full_page=False)

        title = await page.title()
        h1 = await page.locator("h1").first.inner_text() if await page.locator("h1").count() else ""
        print(f"title={title!r} h1={h1!r}")
        print(f"shot={out}")
        if errors:
            print("--- console errors ---")
            for e in errors[:12]:
                print(e)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
