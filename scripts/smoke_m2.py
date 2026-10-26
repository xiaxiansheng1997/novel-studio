"""M2 验收：分块 / FTS5 检索 / 摘要 / S5 注入。

用法：
    .venv\\Scripts\\python.exe -m scripts.smoke_m2
    .venv\\Scripts\\python.exe -m scripts.smoke_m2 --skip-llm   # 不调模型（跳过摘要）

会自己造一个 3 节的作品，第 1 节埋一个只有它有的代号，
然后验证「写第 3 节时能不能检索到第 1 节的那句话」。
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
LOG = BASE_DIR / "storage" / "m2.log"

SEC1 = """调阅申请要先走内网表单，填三个字段：申请人、事由、调阅范围。表单提交后会生成一个流水号，
这个流水号是后续所有沟通的唯一凭据，口头承诺不算。

青岚仓库第三排存着代号 QX-7391 的档案，只有林昭和周叙各持一把钥匙，两把同时插入才能开门。
这条规定是二零一九年那次档案遗失事件之后加的，当时丢了两箱一九九七年的会议纪要，至今没找到。

调阅当天必须两人同时在场，一人开锁一人登记。登记簿是纸质的，不允许拍照，也不允许带出仓库。
如果申请人需要复印件，要在申请表上单独勾选，由周叙亲自复印并在每页骑缝盖章。
"""

SEC2 = """登记簿的格式二十年没变过：日期、姓名、事由、调出编号、归还时间、签字。
纸质的唯一好处是不会被批量篡改，坏处是检索只能靠人翻。周叙自己做过一份电子索引，
只记编号与日期，不记事由，他说事由涉及隐私，不该留副本。

归还环节最容易出问题。有人在登记簿上写了归还时间，实际把档案带回了办公室，
第二天再补送回来。这种事发生过三次，最后一次是二零二二年冬天，涉事的人已经调离。
现在的做法是归还时当场清点页数，并在末页盖一个蓝色「已核」章，没盖章的不算归还。
"""

SEC3_TITLE = "档案调阅流程与责任划分"
SEC3_PROMPT = "写清楚一次完整的调阅要走哪些环节，谁在哪个环节负责，特别是 QX-7391 这类双人钥匙档案的特殊要求。"


def out(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def fail(msg: str) -> None:
    out("FAIL: " + msg)
    raise SystemExit(1)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--skip-llm", action="store_true")
    ap.add_argument("--generate", action="store_true",
                    help="额外跑一次真实生成，验证第 3 节会不会引用第 1 节的设定")
    # 默认跑完就清理：不删的话书架越跑越乱
    ap.add_argument("--keep", action="store_true", help="保留测试作品")
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

    async with httpx.AsyncClient(base_url=args.base_url, timeout=httpx.Timeout(600.0),
                                 headers=headers) as c:
        w = (await c.post("/studio/works", json={
            "kind": "article", "scale": "single", "subtype": "general",
            "title": "档案室调阅规程", "premise": "把纸质调阅流程写清楚，重点讲双人钥匙与责任划分",
            "target_words": 6000,
        })).json()
        wid = w["id"]
        out(f"[1] work -> {wid}")

        nodes = []
        for title, body in (("仓库与钥匙", SEC1), ("登记簿与归还", SEC2), (SEC3_TITLE, "")):
            n = (await c.post(f"/studio/works/{wid}/nodes", json={
                "kind": "section", "title": title, "content": body,
            })).json()
            nodes.append(n)
            out(f"[2] node -> ord={n['ordinal']} {n['title']} {n['word_count']} 字")

        # ---------- 索引 ----------
        r = await c.post(f"/studio/works/{wid}/index", json={"force": True})
        r.raise_for_status()
        idx = r.json()
        out(f"[3] index -> nodes={idx['nodes']} chunks={idx['node_chunks']} "
            f"total={idx['chunks_total']} {idx['elapsed_ms']}ms")
        if idx["chunks_total"] <= 0:
            fail("索引没有产出任何 chunk")

        st = (await c.get(f"/studio/works/{wid}/index/status")).json()
        out(f"[4] status -> body={st['chunks_body']} source={st['chunks_source']} "
            f"indexed={st['nodes_indexed']}/{st['nodes_total']}")

        # ---------- 检索 ----------
        for q in ("QX-7391", "青岚仓库 钥匙", "登记簿 归还"):
            r = await c.post(f"/studio/works/{wid}/search",
                             json={"q": q, "top_k": 5, "scope": "body"})
            r.raise_for_status()
            hits = r.json()["hits"]
            out(f"[5] search {q!r} -> {len(hits)} 条")
            for h in hits[:2]:
                out(f"      ord={h['ordinal']} score={h['score']} {h['title']} | "
                    f"{h['text'][:40].replace(chr(10), ' ')}")
            if q == "QX-7391" and not hits:
                fail("QX-7391 检索不到，FTS5/jieba 链路有问题")

        # ---------- 摘要 ----------
        if not args.skip_llm:
            r = await c.post(f"/studio/nodes/{nodes[0]['id']}/summarize")
            if r.status_code != 200:
                fail(f"摘要失败 {r.status_code}: {r.text[:300]}")
            sm = r.json()["summary"]
            out(f"[6] summary -> {r.json()['chars']} 字")
            for line in sm.splitlines()[:5]:
                out("     " + line)
            if not any(k in sm for k in ("事件", "变化", "钩子")):
                out("     注意：摘要没按三段式输出，模型没完全遵守格式")

        # ---------- 装配预览：第 3 节能否拿到第 1 节的设定 ----------
        r = await c.post(
            f"/studio/context/preview?work_id={wid}&node_id={nodes[2]['id']}",
            json={"task_type": "article_draft", "instruction": SEC3_PROMPT},
        )
        r.raise_for_status()
        ctx = r.json()
        ids = [b["id"] for b in ctx["buckets"]]
        out(f"[7] preview -> 桶={ids} 总量={ctx['total_chars']}")
        for b in ctx["buckets"]:
            out(f"     {b['id']:<6} {b['label']:<8} {b['chars']}/{b['budget']}")
        out(f"     query={ctx.get('query', '')[:80]}")
        evidence = ctx.get("evidence") or []
        out(f"[8] evidence -> {len(evidence)} 条")
        for e in evidence[:3]:
            out(f"     ord={e['ordinal']} score={e['score']} {e['title']}")
        joined = json.dumps(ctx, ensure_ascii=False)
        if "S5" not in ids:
            fail("S5 桶没出现：检索没有注入")
        if "QX-7391" not in joined:
            fail("S5 里没有 QX-7391：第 3 节无法知道第 1 节的设定")
        out("[9] 断言通过：第 3 节的上下文里包含第 1 节的专属设定")

        # ---------- 增量索引：同一内容再跑一次应全部跳过 ----------
        r = await c.post(f"/studio/works/{wid}/index", json={"force": False})
        r.raise_for_status()
        again = r.json()
        out(f"[10] reindex(增量) -> nodes={again['nodes']} skipped={again['skipped']}")
        if again["skipped"] < 2:
            out("     注意：增量索引没有跳过已索引节点，哈希比对可能失效")

        # ---------- 真生成一次：看会不会引用第 1 节的设定 ----------
        if args.generate:
            events: dict[str, int] = {}
            text = ""
            async with c.stream("POST", f"/studio/works/{wid}/pipeline",
                                json={"limit": 1, "only_empty": True,
                                      "pause_ms": 0, "index": True, "summarize": True}) as resp:
                if resp.status_code != 200:
                    fail(f"pipeline {resp.status_code}: {(await resp.aread())[:300]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    ev = json.loads(raw)
                    name = ev.get("event", "?")
                    events[name] = events.get(name, 0) + 1
                    if name == "delta":
                        text += ev.get("text") or ""
            out(f"[11] pipeline -> {events}  正文 {len(text)} 字")
            marks = [k for k in ("QX-7391", "两把钥匙", "双人", "林昭", "周叙", "青岚") if k in text]
            out(f"[12] 引用检查 -> {'命中' if marks else '未命中'} {marks}")
            out("     " + text[:220].replace("\n", " "))
            if not marks:
                out("     注意：模型没引用第 1 节专有信息（检索已注入，可能是模型选择不复用）")

        if not args.keep:
            await c.delete(f"/studio/works/{wid}")
            out("[13] 清理测试作品")

    out("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
