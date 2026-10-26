"""M1 冒烟测试：作品 → 节点 → 上下文预览 →（可选）真生成。

用法：
    .venv\\Scripts\\python.exe -m scripts.smoke_studio
    .venv\\Scripts\\python.exe -m scripts.smoke_studio --generate

不生成时只读接口，不会消耗上游额度；--generate 会真跑一次 deepseek。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent


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
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--generate", action="store_true", help="真跑一次生成（耗时约 10-30 秒）")
    args = ap.parse_args()

    key = os.getenv("W2A_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    base = args.base_url

    async with httpx.AsyncClient(headers=headers, timeout=120) as c:
        r = await c.get(f"{base}/health")
        print(f"[1] health -> {r.status_code} {r.json().get('status')}")

        r = await c.get(f"{base}/studio/templates")
        tpls = r.json()
        print(f"[2] templates -> {len(tpls)} 个：{', '.join(t['name'] for t in tpls[:4])}…")

        r = await c.post(f"{base}/studio/works", json={
            "kind": "article",
            "scale": "single",
            "subtype": "tech",
            "title": "把本地向量检索接进写作服务",
            "premise": "给现有 Python 服务加本地检索，替代 Chroma，重点讲落地步骤与踩坑。",
            "target_words": 5000,
        })
        work = r.json()
        if r.status_code >= 300:
            print(f"[3] 创建作品失败: {r.status_code} {work}")
            return 1
        wid = work["id"]
        print(f"[3] work -> {wid} template={work.get('template_id')}")

        node_ids = []
        for i, title in enumerate(["背景与问题", "方案概述", "落地步骤"], 1):
            r = await c.post(f"{base}/studio/works/{wid}/nodes", json={
                "kind": "section", "title": title,
            })
            n = r.json()
            if r.status_code >= 300:
                print(f"[4] 建节点失败: {r.status_code} {n}")
                return 1
            node_ids.append(n["id"])
        print(f"[4] nodes -> {len(node_ids)} 个，ordinal={[n for n in range(1, len(node_ids) + 1)]}")

        # 给第一节塞一段正文 + 一条素材，验证装配能拿到 S7/S_src
        await c.patch(f"{base}/studio/nodes/{node_ids[0]}", json={
            "content": "现有服务用 Chroma 做检索，部署要带一个独立进程，冷启动 8 秒。"
                       "目标是去掉这个依赖，把检索压进主进程。",
        })
        await c.post(f"{base}/studio/works/{wid}/sources", json={
            "title": "bge-small-zh 实测",
            "content": "bge-small-zh-v1.5 输出 512 维，模型 30MB，8 核 CPU 单条约 20-40ms。"
                       "4300 条 512 维向量暴力检索约 2-5ms。",
            "published_at": "2026-10",
        })

        r = await c.post(f"{base}/studio/context/preview?work_id={wid}&node_id={node_ids[2]}", json={
            "task_type": "article_draft",
            "instruction": "写第 3 节「落地步骤」，分步骤说明安装、建索引、查询。",
        })
        ctx = r.json()
        print(f"[5] preview -> total={ctx['total_chars']} 字")
        for b in ctx["buckets"]:
            print(f"      {b['id']:6} {b['label']:6} {b['chars']:>5}/{b['budget']:<5}")
        for w in ctx["warnings"]:
            print(f"      ! {w}")
        print(f"[6] system={len(ctx['system'])} 字符, user={len(ctx['user'])} 字符")

        if args.generate:
            print("[7] 生成第 3 节（流式）…")
            got = 0
            async with c.stream("POST", f"{base}/studio/nodes/{node_ids[2]}/generate", json={
                "task_type": "article_draft",
                "instruction": "写第 3 节「落地步骤」，分步骤说明安装、建索引、查询，每步给命令。",
                "save": True,
            }) as resp:
                if resp.status_code >= 300:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    print(f"      生成失败 {resp.status_code}: {body[:300]}")
                    return 1
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    ev = json.loads(payload)
                    if ev.get("event") == "meta":
                        print(f"      meta: model={ev['model']} run={ev['run_id']}")
                    elif ev.get("event") == "delta":
                        got += len(ev["text"])
                    elif ev.get("event") == "done":
                        print(f"      完成：{ev['chars']} 字 / {ev['elapsed_ms']}ms saved={ev['saved']}")
                    elif ev.get("event") == "error":
                        print(f"      错误：{ev.get('message')}")
            if got == 0:
                return 1
            r = await c.get(f"{base}/studio/nodes/{node_ids[2]}")
            print(f"[8] 落库校验 -> {len(r.json().get('content') or '')} 字符")

        print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
