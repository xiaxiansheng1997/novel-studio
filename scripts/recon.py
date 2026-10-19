"""接口侦察脚本（命令行兜底）：打开真实页面，用 NetworkRecorder 记录对话请求。

日常更推荐在 Web UI 的"内嵌浏览器"里点【开始录制接口】完成同样的事。
本脚本用于无法使用 UI 或需要脚本化时。

用法：  python -m scripts.recon deepseek
"""
from __future__ import annotations

import asyncio
import sys

from app.browser.network_recorder import RECON_SAMPLE_PROMPT, NetworkRecorder
from app.runtime import Runtime, build_runtime, setup, teardown


async def run(rt: Runtime, key: str) -> None:
    pcfg = rt.config.providers.get(key)
    if pcfg is None:
        print(f"未找到 provider: {key}（可选: {', '.join(rt.config.providers)}）")
        return

    ctx = await rt.pool.context(key, headless=False)
    # 用独立 recon 槽位，避免与内嵌登录页抢同一页面
    page = await rt.pool.get_page(key, "recon")

    rec = NetworkRecorder()
    rec.attach(page)

    await page.goto(pcfg.home_url, wait_until="domcontentloaded")
    print(f"\n请在弹出的浏览器里：")
    print(f"  1) 确认已登录（未登录请先登录，登录态会持久化）")
    print(f"  2) 发送这一句作为样本：{RECON_SAMPLE_PROMPT}")
    print(f"  3) 等回答完全结束后，回到本终端按回车结束抓取\n")
    print(f"  注：若同时开着 uvicorn 服务，两者会共用同一 profile（Chromium 可能报 profile 被占用）；建议先停服务，或直接用 /ui/live.html 的录制开关。\n")
    await asyncio.to_thread(input, ">> 完成上述操作后按回车: ")

    result = rec.write_fixture(key)
    rec.detach()
    print(f"\n[recon] {key}: {result}")
    print("下一步：打开该 fixture 核对 endpoint/headers/body_template/response_sample，")
    print("        据此微调 app/providers/ 下对应适配器的解析器。")


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python -m scripts.recon <provider>  (qwen|doubao|deepseek)")
        return
    key = sys.argv[1]
    rt = build_runtime()

    async def _main():
        await setup(rt)
        try:
            await run(rt, key)
        finally:
            await teardown(rt)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
