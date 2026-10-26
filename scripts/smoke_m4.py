"""M4 验收：任务队列 / 断点 / 暂停恢复 / 抽检卡点 / 自动打分。

用法：
    .venv\\Scripts\\python.exe -m scripts.smoke_m4
    .venv\\Scripts\\python.exe -m scripts.smoke_m4 --chapters 3 --keep

会造一本三章的小说，把「写这三章」排进队列就撒手不管，
然后验证：三章都有正文、都有节拍卡、都有分数、都在队列里留下 done 记录。

抽检阈值调到 2，这样跑两章就会停下来要人看——真实使用是 10。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
LOG = BASE_DIR / "storage" / "m4.log"

CHAPTERS = [
    ("雨夜交割", "玄铁令在青岚仓库交割，林昭与周叙第一次正面试探"),
    ("灶前重算", "周叙夜访，两人对西市口之约各有打算"),
    ("纸灰", "林昭烧掉三日前写的字条，决定不再等"),
]


def out(msg: str) -> None:
    # Windows 控制台默认 GBK，模型输出里带「⚠」这类字符会直接抛
    # UnicodeEncodeError 把整个验收中断（实测踩过）。日志文件用 utf-8，
    # 控制台编不了就替换，不能因为一个字符让整轮验收白跑。
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    enc = sys.stdout.encoding or "utf-8"
    text = msg + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode(enc, "replace").decode(enc, "replace"))
    sys.stdout.flush()


def fail(msg: str) -> None:
    out("FAIL: " + msg)
    raise SystemExit(1)


def db_path() -> Path:
    raw = os.environ.get("W2A_STORAGE_DIR") or "storage"
    p = Path(raw)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p / "studio.sqlite"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--chapters", type=int, default=3)
    ap.add_argument("--triage-every", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    env = BASE_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    key = os.environ.get("W2A_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=httpx.Timeout(120.0),
                                 headers=headers) as c:
        w = (await c.post("/studio/works", json={
            "kind": "novel", "scale": "single", "subtype": "fantasy",
            "title": "玄铁令（队列验收）", "premise": "一枚令符三人转手，谁都在说谎",
            "target_words": 20000,
        })).json()
        wid = w["id"]
        out(f"[1] work -> {wid}")

        nodes = []
        for title, prompt in CHAPTERS[: args.chapters]:
            n = (await c.post(f"/studio/works/{wid}/nodes", json={
                "kind": "chapter", "title": title, "content": ""})).json()
            nodes.append(n)
            out(f"[2] node -> 第{n['ordinal']}章《{n['title']}》")

        # 把大纲要点写进 meta，否则 draft 只知道标题
        meta = {}
        try:
            meta = json.loads(w.get("meta_json") or "{}")
        except json.JSONDecodeError:
            meta = {}
        meta["outline"] = {n["id"]: {"prompt": p, "target_words": 900}
                           for n, (_, p) in zip(nodes, CHAPTERS)}
        await c.patch(f"/studio/works/{wid}", json={"meta_json": json.dumps(meta, ensure_ascii=False)})

        # ---------- 入队 ----------
        body = {
            "task_type": "draft", "only_empty": True, "priority": 9,
            "triage_every": args.triage_every, "auto_beats": True,
        }
        r = await c.post(f"/studio/works/{wid}/batch", json=body)
        if r.status_code != 200:
            fail(f"入队失败 {r.status_code}: {r.text[:300]}")
        q = r.json()
        out(f"[3] enqueue -> queued={q['queued']} note={q['note'] or '-'}")
        if q["queued"] != len(nodes):
            fail(f"应有 {len(nodes)} 个任务，实际 {q['queued']}")

        # ---------- 幂等：同一批再入一次不应重复 ----------
        r = await c.post(f"/studio/works/{wid}/batch", json=body)
        again = r.json()
        out(f"[4] 重复入队 -> queued={again['queued']} deduped={again['deduped']}")
        if again["deduped"] != len(nodes):
            fail("幂等键没生效，同一批任务被重复排队")

        # ---------- 暂停 / 恢复 ----------
        await c.post("/studio/queue/pause", params={"reason": "验收：手动暂停"})
        await asyncio.sleep(2)
        st = (await c.get("/studio/queue/status", params={"work_id": wid})).json()
        out(f"[5] paused -> {st['paused']} reason={st['reason']} "
            f"queued={st['counts'].get('queued', 0)}")
        if not st["paused"]:
            fail("暂停接口没生效")
        await c.post("/studio/queue/resume")
        st = (await c.get("/studio/queue/status", params={"work_id": wid})).json()
        out(f"[6] resumed -> paused={st['paused']} worker={st['worker']}")
        if st["paused"]:
            fail("恢复接口没生效")

        # ---------- 轮询直到跑完 ----------
        started = time.time()
        triage_hits = 0
        last_line = ""
        while time.time() - started < args.timeout:
            st = (await c.get("/studio/queue/status", params={"work_id": wid})).json()
            counts = st["counts"]
            cur = st["current"]
            line = (f"queued={counts.get('queued', 0)} running={counts.get('running', 0)} "
                    f"done={counts.get('done', 0)} failed={counts.get('failed', 0)} "
                    f"eta={st['eta_sec']}s" + (f" · {cur['title']}" if cur else ""))
            if line != last_line:
                out(f"     {line}")
                last_line = line

            if st["triage"]["due"]:
                triage_hits += 1
                out(f"[7] 抽检卡点触发 #{triage_hits}")
                for s in st["triage"]["samples"]:
                    out(f"     {s['title']} score={s['score']}")
                st = (await c.post("/studio/queue/triage", json={"verdict": "ok"})).json()
                out(f"     判定 ok -> paused={st['paused']}")
                continue

            if st["paused"]:
                fail(f"队列意外暂停：{st['reason']}")

            if not cur and not counts.get("queued") and not counts.get("running"):
                break
            await asyncio.sleep(3)
        else:
            fail(f"{args.timeout}s 内没跑完")

        elapsed = int(time.time() - started)
        out(f"[8] 队列跑完 -> 用时 {elapsed}s，抽检 {triage_hits} 次")

        # ---------- 断言：产物 ----------
        tree = (await c.get(f"/studio/works/{wid}/tree")).json()
        readable = sorted([n for n in tree if n.get("ordinal") is not None],
                          key=lambda n: n["ordinal"])
        full = []
        for item in readable:
            node = (await c.get(f"/studio/nodes/{item['id']}")).json()
            full.append(node)
        written = [n for n in full if (n.get("content") or "").strip()]
        beat_cards = [n for n in full if (n.get("summary") or "").strip()]
        out(f"[9] 产物 -> 正文 {len(written)}/{len(full)} 章，节拍卡 {len(beat_cards)}/{len(full)} 章")
        for n in full:
            out(f"     第{n['ordinal']}章《{n['title']}》正文 {n.get('word_count') or 0} 字，"
                f"节拍卡 {(n.get('summary') or '')[:24]}…")
        if len(written) != len(full):
            fail("有章节没写出正文")
        if len(beat_cards) != len(full):
            fail("有章节没生成节拍卡（beats 应该先于 draft 自动补）")

        # ---------- 断言：打分 ----------
        scores = (await c.get(f"/studio/works/{wid}/scores")).json()
        out(f"[10] 打分 -> {len(scores)} 条")
        for s in scores[:6]:
            out(f"     {s['node_id']} score={s['score']} chars={s['chars']} {s['task_type']}")
        if len(scores) < len(full):
            fail("有些章节没有分数，runs.score 没写进去")
        if any(s["score"] is None for s in scores[: len(full)]):
            fail("分数为空的记录混进来了")

        # ---------- 断言：索引与图谱 ----------
        st_idx = (await c.get(f"/studio/works/{wid}/index/status")).json()
        out(f"[11] 索引 -> {st_idx['chunks_body']} 块 / {st_idx['nodes_indexed']} 节")
        if st_idx["chunks_body"] <= 0:
            fail("写完了但没建索引，后面的章节检索不到")

        g = (await c.get(f"/studio/works/{wid}/graph")).json()
        out(f"[12] 图谱 -> {g['stats']}")
        if g["stats"]["entities"] <= 0:
            fail("写完了但没抽设定，S6 永远是空的")

        # 节拍卡是否复用了已抽出的人物（不硬性失败：第一章时图谱还是空的）
        names = {e["name"] for e in g["entities"] if e["kind"] == "character"}
        reuse = [n["title"] for n in full
                 if any(name in (n.get("summary") or "") for name in names)]
        out(f"[12b] 节拍卡复用人物 -> {len(reuse)}/{len(full)} 章 {sorted(names)[:6]}")
        if names and len(reuse) < len(full):
            fail("有章节的节拍卡没提到任何已有人物，模型在自造人名")

        # ---------- 断点恢复 ----------
        with sqlite3.connect(db_path(), timeout=10) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'running' WHERE work_id = ? AND status = 'done' "
                "AND rowid = (SELECT MIN(rowid) FROM jobs WHERE work_id = ? AND status = 'done')",
                (wid, wid))
            touched = cur.rowcount or 0
            conn.commit()
        r = await c.post("/studio/queue/recover")
        rec = r.json()
        out(f"[13] 断点恢复 -> 人为制造 {touched} 个 running，回收 {rec['recovered']} 个")
        if touched and rec["recovered"] < 1:
            fail("卡在 running 的任务没被回收，重启后会永远不动")

        # ---------- 失败任务重试入口 ----------
        jobs = (await c.get("/studio/jobs", params={"work_id": wid, "limit": 50})).json()
        types = {}
        for j in jobs:
            types[j["task_type"]] = types.get(j["task_type"], 0) + 1
        out(f"[14] jobs -> {len(jobs)} 条 {types}")
        if types.get("draft", 0) < len(full):
            fail("draft 任务记录缺失")

        if not args.keep:
            # 先停一下再删：断点恢复那步把一条 done 改回了 queued，
            # 不拦住的话 worker 会拿它去重写刚被删掉的作品，然后连续失败。
            await c.post("/studio/queue/pause", params={"reason": "验收收尾"})
            with sqlite3.connect(db_path(), timeout=10) as conn:
                conn.execute("DELETE FROM jobs WHERE work_id = ?", (wid,))
                conn.commit()
            await c.delete(f"/studio/works/{wid}")
            # 收尾把队列放回运行状态，否则下一次启动像「卡住」
            await c.post("/studio/queue/resume")
            out("[15] 清理测试作品")

    out("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
