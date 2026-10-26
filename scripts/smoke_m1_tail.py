"""M1 尾款验收：类型配置 / 大纲 / 逐节流水线 / 拼装导出 / 加工多候选。

用法：
    .venv\\Scripts\\python.exe -m scripts.smoke_m1_tail
    .venv\\Scripts\\python.exe -m scripts.smoke_m1_tail --sections 2 --candidates 2
    .venv\\Scripts\\python.exe -m scripts.smoke_m1_tail --skip-llm   # 只验证不调模型的部分

会真跑一次上游（约 1-2 分钟），产物留在 studio.sqlite 里可手动删。
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


LOG = BASE_DIR / "storage" / "m1tail.log"


def out(msg: str) -> None:
    # Windows 控制台默认 GBK，中文会炸；同时写一份 UTF-8 日志方便直接看文件
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--sections", type=int, default=2, help="流水线只跑前 N 节")
    ap.add_argument("--candidates", type=int, default=2)
    ap.add_argument("--skip-llm", action="store_true")
    # 默认跑完就把测试作品删掉：不删的话书架越跑越乱
    ap.add_argument("--keep", action="store_true", help="保留测试作品")
    args = ap.parse_args()

    load_env()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    key = os.environ.get("W2A_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    timeout = httpx.Timeout(600.0, connect=20.0)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout, headers=headers) as c:
        # ---------- 1. 类型配置 ----------
        r = await c.get("/studio/content-types")
        r.raise_for_status()
        types = r.json()
        out(f"[1] content-types -> {len(types['kinds'])} kinds  {types.get('error') or ''}")
        for k in types["kinds"]:
            scales = ",".join(s["id"] for s in k["scales"])
            subs = ",".join(s["id"] for s in k["subtypes"])
            out(f"    {k['id']:<8} {k['label']:<6} scales=[{scales}] subtypes=[{subs}]")

        r = await c.post("/studio/content-types/reload")
        r.raise_for_status()
        j = r.json()
        out(f"[2] reload -> kinds={j['kinds']} templates={j['templates']}")

        # ---------- 2. 建一个系列型技术作品 ----------
        w = (await c.post("/studio/works", json={
            "kind": "article", "scale": "series", "subtype": "tech",
            "title": "本地检索接入写作服务", "premise": "用 numpy 暴力检索替代 Chroma，去掉独立进程依赖",
            "target_words": 12000,
        })).json()
        wid = w["id"]
        out(f"[3] work -> {wid} scale={w['scale']} template={w['template_id']}")

        r = await c.post(f"/studio/works/{wid}/sources", json={
            "title": "bge-small-zh 实测记录",
            "published_at": "2026-10",
            "content": "512 维向量，单条约 1.2KB。10 万段规模下 numpy 暴力点积约 8ms，"
                       "不需要独立向量库进程；Chroma 冷启动约 8 秒，是主要痛点。",
        })
        r.raise_for_status()
        out(f"[4] source -> {r.json()['id']}")

        if args.skip_llm:
            out("--skip-llm：跳过模型相关步骤")
            return 0

        # ---------- 3. 生成大纲 ----------
        t0 = asyncio.get_event_loop().time()
        r = await c.post(f"/studio/works/{wid}/outline", json={
            "instruction": "面向有 Python 基础的后端，重点讲落地步骤与坑",
        })
        if r.status_code != 200:
            out(f"[5] outline FAILED {r.status_code}: {r.text[:400]}")
            return 1
        ol = r.json()
        out(f"[5] outline -> {ol['sections']} 节 / {len(ol['nodes'])} 节点 / {ol['elapsed_ms']}ms")
        for n in ol["nodes"][:12]:
            out(f"    {n['kind']:<8} ord={n['ordinal']}  {n['title']}")

        tree = (await c.get(f"/studio/works/{wid}/tree")).json()
        readable = [n for n in tree if n["ordinal"] is not None]
        out(f"[6] tree -> {len(tree)} 节点，其中可读 {len(readable)} 节")

        # ---------- 4. 逐节生成 ----------
        events: dict[str, int] = {}
        node_chars: list[int] = []
        started = asyncio.get_event_loop().time()
        async with c.stream("POST", f"/studio/works/{wid}/pipeline",
                            json={"limit": args.sections, "pause_ms": 300}) as resp:
            if resp.status_code != 200:
                out(f"[7] pipeline FAILED {resp.status_code}: {(await resp.aread())[:400]}")
                return 1
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                ev = json.loads(raw)
                name = ev.get("event", "?")
                events[name] = events.get(name, 0) + 1
                if name == "node_done":
                    node_chars.append(ev["chars"])
        elapsed = asyncio.get_event_loop().time() - started
        out(f"[7] pipeline -> {events}  用时 {elapsed:.1f}s  每节字数={node_chars}")

        # ---------- 5. 拼装 + 导出 ----------
        r = await c.post(f"/studio/works/{wid}/assemble")
        r.raise_for_status()
        doc = r.json()
        out(f"[8] assemble -> {doc['chars']} 字符 / {doc['nodes']} 节")
        out("    " + doc["markdown"][:160].replace("\n", " / "))

        r = await c.get(f"/studio/works/{wid}/export")
        r.raise_for_status()
        out(f"[9] export -> {len(r.content)} bytes  type={r.headers.get('content-type')} "
            f"nodes={r.headers.get('x-nodes-used')}")

        # ---------- 6. 加工多候选 ----------
        r = await c.post("/studio/transform", json={
            "text": "这个方案很好。它使用了 numpy 来做检索。它去掉了 Chroma。它的速度快了很多。",
            "action": "rewrite",
            "candidates": args.candidates,
            "work_id": wid,
        })
        if r.status_code != 200:
            out(f"[10] transform FAILED {r.status_code}: {r.text[:400]}")
            return 1
        tr = r.json()
        out(f"[10] transform -> {tr['label']} / {len(tr['candidates'])} 候选 / errors={tr['errors']}")
        for cand in tr["candidates"]:
            out(f"     候选{cand['variant'] + 1}: {cand['chars']}字 {cand['elapsed_ms']}ms")
            out(f"       {cand['text'][:80]}…")

        # ---------- 7. 二次拼装（含空节）应同样成立 ----------
        r = await c.post(f"/studio/works/{wid}/assemble?include_empty=true")
        r.raise_for_status()
        out(f"[11] assemble(include_empty) -> {r.json()['nodes']} 节")

        if not args.keep:
            await c.delete(f"/studio/works/{wid}")
            out("[12] 清理测试作品")

    out("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
