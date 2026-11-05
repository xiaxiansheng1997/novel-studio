"""检索评测：把「这次改动有没有变好」变成可重复的数字。

用法：
    .venv\\Scripts\\python.exe -m scripts.measure_retrieval
    .venv\\Scripts\\python.exe -m scripts.measure_retrieval --work-id w_xxx --limit 50
    .venv\\Scripts\\python.exe -m scripts.measure_retrieval --probe "令底偏位两分=>令底"
    .venv\\Scripts\\python.exe -m scripts.measure_retrieval --db storage/backups/xxx.sqlite
    .venv\\Scripts\\python.exe -m scripts.measure_retrieval --json storage/m0/before.json

不调模型；`--db` 指到其它库时先抄一份临时副本（`init()` 会补列/回填/同步模板，
直接开备份等于把基线改掉）。默认挑块数最多的那本书（长篇才看得出问题）。

指标（对每个有正文的节跑一遍，最后取平均）：

  terms/match   计划里抽出多少个检索词、其中多少个真的进了 MATCH
  related@k     top-k 里与本节计划共享 ≥2 个词的块数（4 词以下门槛降为 1）
  sum           其中来自摘要通道的条数（P0-2 的收益就看这一列）
  gap           含近因重排后，top-k 与当前节的节号距离中位数
  bm25_gap      同一批候选只按 BM25 排时的距离中位数
  recent6       top-k 里落在「最近 6 节」的比例（S4 已经注入过这些节，重复占预算）

`gap < bm25_gap` 说明近因把远处的证据往后推了——差得越多，P1 的乘性衰减越该做。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "storage" / "studio.sqlite"


def out(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 3) if values else 0.0


def _median(values: list[int]) -> float | None:
    return round(statistics.median(values), 2) if values else None


async def pick_work(store, work_id: str) -> dict | None:
    if work_id:
        return await store.get_work(work_id)
    best: dict | None = None
    best_n = -1
    for w in await store.list_works(limit=200):
        n = await store.count_chunks(w["id"], "body")
        if n > best_n:
            best, best_n = w, n
    return best


async def eval_node(store, work: dict, node: dict, args) -> dict:
    from app.studio import prompting, retrieval, tokenize

    meta = prompting.work_meta(work)
    terms = prompting.retrieval_terms(node, meta)
    query = retrieval.build_query(*terms, work.get("premise"))
    match = tokenize.match_query(query)
    current = int(node.get("ordinal") or 0) or None

    hits = await retrieval.retrieve(
        store,
        work["id"],
        query=query,
        current_ordinal=current,
        kinds=("body",),
        top_k=args.top_k,
        budget_chars=args.budget,
        exclude_node_ids={node["id"]},
        # 生产 S5 传了短语加成，这里不传就量不到 P2 的收益
        phrases=prompting.retrieval_phrases(node, meta),
    )
    # 同一批候选只按 BM25 排的对照：近因的影响就是两者之差
    raw = await store.search_chunks(
        work["id"], match, kinds=("body",), limit=max(args.top_k * 4, 24)
    ) if match else []
    raw = [r for r in raw if r.get("node_id") != node["id"]][: args.top_k]

    qterms = set(terms)
    need = 2 if len(qterms) >= 4 else 1
    related = sum(
        1 for h in hits
        if len(qterms & set(tokenize.tokens(h.get("text") or ""))) >= need
    )
    gaps = [abs(int(h.get("ordinal") or 0) - current) for h in hits
            if h.get("ordinal") and current]
    recent6 = sum(1 for g in gaps if 0 < g <= 6)

    return {
        "ordinal": current,
        "title": node.get("title") or "",
        "terms": len(terms),
        "match": match.count(" OR ") + 1 if match else 0,
        "hits": len(hits),
        "related": related,
        "sum_hits": sum(1 for h in hits if h.get("source_kind") == "summary"),
        "gap": _median(gaps),
        "bm25_gap": _median([
            abs(int(r.get("ordinal") or 0) - current) for r in raw
            if r.get("ordinal") and current
        ]),
        "recent6": round(recent6 / len(gaps), 3) if gaps else None,
        "query": query,
        "top": [
            {"ordinal": h.get("ordinal"), "title": h.get("title"),
             "score": h.get("score"), "chars": len(h.get("text") or "")}
            for h in hits[:5]
        ],
    }


async def run_probe(store, work: dict, spec: str, at: int, args) -> dict:
    from app.studio import retrieval

    query, _, want = spec.partition("=>")
    query, want = query.strip(), want.strip()
    hits = await retrieval.retrieve(
        store, work["id"], query=query, current_ordinal=at or None,
        kinds=("body",), top_k=args.top_k, budget_chars=args.budget,
    )
    # 对照：`current_ordinal=None` 会同时关掉近因与摘要路（摘要窗口要靠它算），
    # 所以这一列量的是「只有正文 BM25」的基线，不是「只去掉近因」
    plain = await retrieval.retrieve(
        store, work["id"], query=query, current_ordinal=None,
        kinds=("body",), top_k=args.top_k, budget_chars=args.budget,
    )

    def rank(items: list[dict]) -> int:
        if not want:
            return 0
        for i, h in enumerate(items, 1):
            if want in (h.get("title") or "") or want in (h.get("text") or ""):
                return i
        return 0

    return {
        "query": query,
        "want": want,
        "rank": rank(hits),
        "rank_no_recency": rank(plain),
        "top": [
            {"ordinal": h.get("ordinal"), "title": h.get("title"), "score": h.get("score")}
            for h in hits[:5]
        ],
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH), help="默认 storage/studio.sqlite")
    ap.add_argument("--work-id", default="", help="不填则挑正文块最多的那本")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--budget", type=int, default=6000)
    ap.add_argument("--limit", type=int, default=0, help="只评测前 N 节（0 = 全部）")
    ap.add_argument("--probe", action="append", default=[],
                    help='手写探针，格式 "查询词=>期望命中的标题/正文词"，可多次传')
    ap.add_argument("--probe-at", type=int, default=0,
                    help="探针的当前节号；默认用最后一节，用来观察近因的影响")
    ap.add_argument("--json", default="", help="把完整结果写到这个文件，方便改动前后对比")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        out(f"FAIL: 找不到 {db}")
        return 1

    # 这个脚本建索引状态（`init()` 会跑 _migrate 补列/回填、_seed_templates 覆盖内置模板），
    # 直接开备份等于把「改动前的基线」改掉。指到别的库时先抄一份临时的。
    tmpdir = ""
    target = db
    if db.resolve() != DB_PATH.resolve():
        tmpdir = tempfile.mkdtemp(prefix="measure-retrieval-")
        target = Path(tmpdir) / "studio.sqlite"
        shutil.copy2(db, target)

    from app.studio.store import StudioStore

    store = StudioStore(target)
    await store.init()
    try:
        work = await pick_work(store, args.work_id)
        if not work:
            out("FAIL: 没有作品")
            return 1
        nodes = [n for n in await store.list_readable(work["id"]) if (n.get("chars") or 0) > 0]
        if args.limit:
            nodes = nodes[: args.limit]
        if not nodes:
            out(f"FAIL: 《{work.get('title')}》没有带正文的节（先建索引）")
            return 1

        chunks = await store.count_chunks(work["id"], "body")
        out(f"作品：{work.get('title')}  id={work['id']}  kind={work.get('kind')}")
        out(f"规模：{len(nodes)} 节 / {chunks} 块 / 库 {db.stat().st_size / 1048576:.1f} MB")
        out(f"参数：top_k={args.top_k} budget={args.budget}"
            + (f" limit={args.limit}" if args.limit else ""))
        out()

        rows: list[dict] = []
        header = (f"{'节':>5}  {'terms':>5}{'match':>6}{'related':>8}{'sum':>5}"
                  f"{'gap':>7}{'bm25':>7}{'recent6':>9}  标题")
        out(header)
        out("-" * 90)
        for item in nodes:
            full = await store.get_node(item["id"])
            if not full:
                continue
            row = await eval_node(store, work, full, args)
            rows.append(row)
            gap = "-" if row["gap"] is None else f"{row['gap']:.1f}"
            bgap = "-" if row["bm25_gap"] is None else f"{row['bm25_gap']:.1f}"
            r6 = "-" if row["recent6"] is None else f"{row['recent6']:.2f}"
            out(f"{row['ordinal'] or 0:>5}  {row['terms']:>5}{row['match']:>6}"
                f"{row['related']:>8}{row['sum_hits']:>5}{gap:>7}{bgap:>7}{r6:>9}  "
                f"{row['title'][:28]}")
        out("-" * 90)

        summary = {
            "nodes": len(rows),
            "terms_avg": _mean([r["terms"] for r in rows]),
            "match_avg": _mean([r["match"] for r in rows]),
            "hits_avg": _mean([r["hits"] for r in rows]),
            "related_avg": _mean([r["related"] for r in rows]),
            "sum_hits_avg": _mean([r["sum_hits"] for r in rows]),
            "gap_avg": _mean([r["gap"] for r in rows if r["gap"] is not None]),
            "bm25_gap_avg": _mean([r["bm25_gap"] for r in rows if r["bm25_gap"] is not None]),
            "recent6_avg": _mean([r["recent6"] for r in rows if r["recent6"] is not None]),
        }
        out(f"平均：terms={summary['terms_avg']} match={summary['match_avg']} "
            f"hits={summary['hits_avg']}/{args.top_k} related={summary['related_avg']} "
            f"sum={summary['sum_hits_avg']} "
            f"gap={summary['gap_avg']} bm25_gap={summary['bm25_gap_avg']} "
            f"recent6={summary['recent6_avg']}")
        drift = summary["bm25_gap_avg"] - summary["gap_avg"]
        verdict = "近因把证据向当前节拉近了" if drift > 0.5 else "近因影响不大"
        out(f"      gap 差 {drift:+.2f} → {verdict}")
        from app.studio import retrieval as _r
        if len(nodes) <= _r.RECENT_WINDOW:
            # 不写这一句，sum=0 会被读成「摘要通道没生效」——实际是这几节全在 S4 的窗口里
            out(f"      节数 {len(nodes)} ≤ 最近 {_r.RECENT_WINDOW} 节，"
                f"摘要通道整段让给 S4（sum 恒为 0 是预期的）")

        probes: list[dict] = []
        if args.probe:
            at = args.probe_at or int(nodes[-1].get("ordinal") or 0)
            out()
            out(f"探针（current_ordinal={at}）：")
            for spec in args.probe:
                got = await run_probe(store, work, spec, at, args)
                probes.append(got)
                out(f"  {got['query']}  → 期望含「{got['want']}」")
                out(f"      含近因 rank={got['rank'] or '未召回'}  "
                    f"仅 BM25 rank={got['rank_no_recency'] or '未召回'}")
                for i, h in enumerate(got["top"], 1):
                    out(f"      {i}. 第 {h['ordinal']} 节 {h['title']}  score={h['score']}")

        payload = {
            "work": {"id": work["id"], "title": work.get("title"), "kind": work.get("kind")},
            "db": str(db),
            "params": {"top_k": args.top_k, "budget": args.budget},
            "summary": summary,
            "rows": rows,
            "probes": probes,
        }
        if args.json:
            dest = Path(args.json)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            out(f"\n已写入 {dest}")
        return 0
    finally:
        await store.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
