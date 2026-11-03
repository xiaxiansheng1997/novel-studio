"""迭代 C：最小前端冒烟——真服务 + 无头 Chromium 打开写作台与控制台。

查的是装配层：静态挂载、SPA 起得来、首屏渲染非空、全程没有 JS 报错。
业务正确性由 pytest（后端）与 Vitest（组件）负责，这一步只保证
「构建出来的东西能在真实浏览器里跑起来」。

跑在隔离环境里（审查 m11）：storage 指向临时目录、API key 用冒烟专用假值——
不然 lifespan 会往真实 storage 写快照，历史里残留的 queued 任务甚至会真的
去调模型。缺依赖/缺浏览器时 SKIP 返回码是 2 而不是 0：调用方（check.ps1）
得能分辨「没跑」和「跑了且通过」，不许把没跑汇进「全部通过」（审查 M3）。

用法：
    .venv\\Scripts\\python.exe -m scripts.smoke_frontend

frontend/dist 没构建、playwright 没装或浏览器缺失时跳过并返回 2：
check.ps1 在别的机器上不该被浏览器安装卡死。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DIST = BASE_DIR / "frontend" / "dist"

SKIP_RC = 2


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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    if not (DIST / "index.html").exists():
        print("SKIP：frontend/dist 不存在，先跑 npm run build")
        return SKIP_RC
    try:
        import httpx
        import uvicorn
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"SKIP：缺依赖（{e.name}），前端冒烟需要 playwright")
        return SKIP_RC

    load_env()
    # 环境隔离：环境变量优先于 .env（pydantic-settings 的覆盖链）。
    # storage 指到临时库、口令换成冒烟假值：不这样做，lifespan 会往
    # 真实 storage 写快照，queued 任务还会真的去调模型（审查 m11）。
    os.environ["W2A_STORAGE_DIR"] = tempfile.mkdtemp(prefix="novel-studio-smoke-")
    os.environ["W2A_API_KEY"] = "smoke-local-key"
    # 自带一个干净端口的服务：不复用 8000，免得跟开发中的 dev.py 抢
    port = free_port()
    from app.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.25)
    else:
        print(f"FAIL：服务没起来（{base}）")
        server.should_exit = True
        return 1

    rc = 0
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"SKIP：Chromium 起不来（多半没装浏览器）：{str(e)[:120]}")
                return SKIP_RC
            errors: list[str] = []
            try:
                page = browser.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                key = os.getenv("W2A_API_KEY", "")
                # 服务端设了口令时替浏览器带上：书架拉的可是受保护的接口
                page.add_init_script(
                    f"localStorage.setItem('w2a_apikey', {json.dumps(key)})")

                for path, label in [("/studio", "写作台"), ("/admin", "控制台")]:
                    resp = page.goto(f"{base}{path}", wait_until="load", timeout=30000)
                    if resp is None or not resp.ok:
                        print(f"FAIL：{label} {path} 返回 "
                              f"{getattr(resp, 'status', None)}")
                        return 1
                    # SPA 起没起来看 #app：挂载后至少有一个子节点（哪怕是无权限引导页）
                    page.wait_for_function(
                        "document.querySelector('#app')?.children.length > 0",
                        timeout=15000)
                    print(f"[{label}] {path} -> 渲染 OK")

                # 阅读页真访问：scoped CSS 变量、目录高亮这类问题只有真浏览器
                # 渲染才能发现，冒烟不能对自己要保护的功能是瞎的（审查 m12）。
                # 临时库里没书就用不存在的 id：验证的是「没内容也能降级渲染」。
                work_id = ""
                try:
                    r = httpx.get(f"{base}/studio/works",
                                  headers={"Authorization": f"Bearer {key}"}, timeout=5)
                    if r.status_code == 200 and r.json():
                        work_id = str(r.json()[0].get("id") or "")
                except Exception:  # noqa: BLE001 - 拿不到书不致使命名场景挂掉
                    pass
                read_path = f"/studio/{work_id or 'w_none'}/read"
                resp = page.goto(f"{base}{read_path}", wait_until="load", timeout=30000)
                if resp is None or not resp.ok:
                    print(f"FAIL：阅读页 {read_path} 返回 {getattr(resp, 'status', None)}")
                    return 1
                page.wait_for_function(
                    "document.querySelector('#app')?.children.length > 0", timeout=15000)
                print(f"[阅读页] {read_path} -> 渲染 OK")

                # 深链接走 SPA fallback：刷新 /studio/xxx 不该看到 JSON 404
                resp = page.goto(f"{base}/studio/w_smoke", wait_until="load")
                html = page.content()
                if not (resp and resp.ok and "<div id=\"app\"" in html):
                    print("FAIL：作品深链接没走 SPA fallback")
                    return 1
                print("[深链接] /studio/<id> -> index.html OK")
            finally:
                browser.close()
            if errors:
                print(f"FAIL：页面 JS 报错 {len(errors)} 条，前两条：")
                for e in errors[:2]:
                    print(f"      {e[:300]}")
                rc = 1
    finally:
        server.should_exit = True

    if rc == 0:
        print("OK")
    return rc


if __name__ == "__main__":
    sys.exit(main())
