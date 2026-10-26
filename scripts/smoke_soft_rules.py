"""软规则误报基线：拿真实作品正文跑一遍，只看报什么，不落库、不调模型。

用法：
    .venv\\Scripts\\python.exe -m scripts.smoke_soft_rules               # 跑默认语料
    .venv\\Scripts\\python.exe -m scripts.smoke_soft_rules --work-id w_xxx
    .venv\\Scripts\\python.exe -m scripts.smoke_soft_rules --all          # 书架上全部作品

为什么要单独一个脚本：软规则的验收标准不是「能报出来」（单测已经覆盖了），
而是「在真实正文里不吵人」。吵人的规则会被作者整组关掉，那硬规则也没人看了。

默认按每个作品自己的 `checks` 配置跑（和生产路径一致），所以结果才是「实际会
看到几条」。本脚本只调用 soft_rules 的纯函数，不写 issues 表，随便跑。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "storage" / "studio.sqlite"

DEFAULT_WORK = "w_e85bbb77c0b74016"


def out(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


async def run_one(store, work: dict, force_rules: set[str]) -> int:
    from app.studio import content_types, soft_rules

    if force_rules:
        rules = force_rules
        src = "--force-rules"
    else:
        resolved = content_types.resolve(work.get("kind") or "article",
                                         work.get("scale") or "", work.get("subtype") or "")
        rules = set(resolved.get("checks") or [])
        src = "作品配置"
    out(f"\n=== {work['id']} {work['title']} kind={work.get('kind')} "
        f"subtype={work.get('subtype') or '-'} 规则({src})={sorted(rules & soft_rules_all())} ===")
    entities = await store.list_entities(work["id"])
    cards = await store.list_cards(work["id"])
    sources = await store.list_sources(work["id"], limit=10)
    out(f"实体 {len(entities)} / 卡片 {len(cards)} / 素材 {len(sources)}")

    total_chars = 0
    hits: list[dict] = []
    for item in await store.list_readable(work["id"]):
        node = await store.get_node(item["id"])
        if not node:
            continue
        content = (node.get("content") or "").strip()
        if not content:
            continue
        total_chars += len(content)
        ordinal = int(node.get("ordinal") or 0)
        found = await soft_rules.check_node(store, work, node, content, ordinal, rules)
        for f in found:
            f["where"] = f"第 {ordinal} 节 {node.get('title') or ''}"
        hits += found

    book = await soft_rules.check_whole_book(store, work, rules)
    for f in book:
        f["where"] = f"整本对账 位于第 {f.get('location')} 节"
    hits += book

    out(f"正文 {total_chars} 字，命中 {len(hits)} 条")
    for h in hits:
        out(f"  [{h['rule']}/{h['severity']}] {h.get('where')}")
        out(f"      {h['message']}")
        if h.get("evidence"):
            out(f"      证据：{h['evidence'][:70]}")
    if total_chars and not hits:
        out("  （零命中：要么真干净，要么规则没跑起来——单测负责证明后者）")
    return len(hits)


def soft_rules_all() -> set[str]:
    from app.studio import soft_rules

    return set(soft_rules.NOVEL_SOFT + soft_rules.DOC_SOFT)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work-id", default=DEFAULT_WORK)
    ap.add_argument("--all", action="store_true", help="书架上全部作品")
    ap.add_argument("--force-rules", default="",
                    help="逗号分隔：无视作品配置强跑这几条（会看到一堆不在生产路径上的命中）")
    args = ap.parse_args()

    if not DB_PATH.exists():
        out(f"FAIL: 找不到 {DB_PATH}")
        return 1
    from app.studio.store import StudioStore

    force = {r.strip() for r in args.force_rules.split(",") if r.strip()}
    store = StudioStore(DB_PATH)
    await store.init()
    try:
        works = await store.list_works(limit=200)
        if args.all:
            targets = works
        else:
            targets = [w for w in works if w["id"] == args.work_id]
            if not targets:
                out(f"FAIL: 书架上没有 {args.work_id}（--all 可以看全部）")
                return 1
        total = 0
        for w in targets:
            total += await run_one(store, w, force)
        out(f"\n合计命中 {total} 条，作品 {len(targets)} 个")
    finally:
        await store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
