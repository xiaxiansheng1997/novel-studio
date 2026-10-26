"""清书架：批量删掉测试作品，删之前先备份数据库。

用法：
    .venv\\Scripts\\python.exe -m scripts.cleanup_works              # 只列，不动手
    .venv\\Scripts\\python.exe -m scripts.cleanup_works --yes        # 删默认保留以外的全部
    .venv\\Scripts\\python.exe -m scripts.cleanup_works --keep w_a,w_b --yes
    .venv\\Scripts\\python.exe -m scripts.cleanup_works --only-id w_xxx --yes
    .venv\\Scripts\\python.exe -m scripts.cleanup_works --sweep-orphans --yes

为什么留一个默认保留项：`w_e85bbb77c0b74016` 是唯一带正文 + 图谱（17 实体 / 20 关系
/ 19 事件）的语料，软规则误报基线（`smoke_soft_rules.py`）和图谱可视化都靠它验收。
删完之后这两件事只能靠人造数据，成本比留一本高。

删除走 `store.delete_work()`，与 HTTP 接口同一条路径：`works` 上的级联外键负责
nodes/chunks/summaries/实体等；`jobs` / `runs` / `index_units` 的 work_id 上没有外键、
FTS5 虚拟表 `chunks_fts` 不吃外键，这四张都由 delete_work 手动清。
`--sweep-orphans` 是补票入口：清掉那些在修复之前删作品留下的孤儿行。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "storage" / "studio.sqlite"
BACKUP_DIR = BASE_DIR / "storage" / "backups"

# 验收语料，默认不删（详见模块头）
KEEP_DEFAULT = ("w_e85bbb77c0b74016",)


def out(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _table(rows: list[dict]) -> list[str]:
    lines = [f"{'作品 id':<22}{'kind':<8}{'节':>4}{'字数':>8}{'实体':>6}{'关系':>6}"
             f"{'素材':>6}  标题 / 更新时间"]
    for w in rows:
        lines.append(
            f"{w['id']:<22}{(w.get('kind') or ''):<8}{w.get('nodes', 0):>4}"
            f"{w.get('words', 0):>8}{w.get('ents', 0):>6}{w.get('rels', 0):>6}"
            f"{w.get('srcs', 0):>6}  {(w.get('title') or '')[:24]}  "
            f"{datetime.fromtimestamp(w.get('updated_at') or 0):%m-%d %H:%M}"
        )
    return lines


async def collect(store) -> list[dict]:
    rows = []
    for w in await store.list_works(limit=200):
        stats = await store.graph_stats(w["id"])
        rows.append({
            **w,
            "nodes": len(await store.list_readable(w["id"])),
            "ents": stats["entities"],
            "rels": stats["relations"],
            "srcs": len(await store.list_sources(w["id"], limit=200)),
        })
    return rows


def maybe_backup(args) -> None:
    """删之前先拷一份：删作品和清孤儿都没有撤销键。"""
    if args.no_backup:
        out("\n警告：--no-backup，删完没法回滚")
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"studio-before-cleanup-{datetime.now():%Y%m%d_%H%M%S}.sqlite"
    shutil.copy2(DB_PATH, dest)
    out(f"\n已备份到 {dest.relative_to(BASE_DIR)}")


async def sweep(store, args) -> int:
    """只清孤儿行（指向已不存在作品的 jobs/runs/index_units/chunks_fts）。"""
    before = await store.orphan_counts()
    if not before:
        out("孤儿行：没有，库里是干净的")
        return 0
    out(f"孤儿行：{before}")
    if not args.yes:
        out("\n（试运行：什么也没删。加 --yes 清掉）")
        return 0
    maybe_backup(args)
    deleted = await store.sweep_orphans()
    out(f"已清理 {deleted}")
    left = await store.orphan_counts()
    out(f"清理后剩余孤儿：{left or '没有'}")
    return 0 if not left else 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", default=",".join(KEEP_DEFAULT),
                    help="逗号分隔的作品 id，默认保留验收语料")
    ap.add_argument("--only-id", default="", help="只删这几个 id（逗号分隔），忽略保留逻辑")
    ap.add_argument("--yes", action="store_true", help="真正执行删除")
    ap.add_argument("--no-backup", action="store_true",
                    help="跳过备份（不推荐：删了没有撤销键）")
    ap.add_argument("--sweep-orphans", action="store_true",
                    help="不删作品，只清掉 jobs/runs/index_units/chunks_fts 里的孤儿行")
    args = ap.parse_args()

    if not DB_PATH.exists():
        out(f"FAIL: 找不到 {DB_PATH}")
        return 1

    from app.studio.store import StudioStore

    keep = {k.strip() for k in args.keep.split(",") if k.strip()}
    store = StudioStore(DB_PATH)
    await store.init()
    try:
        if args.sweep_orphans:
            return await sweep(store, args)

        rows = await collect(store)
        out(f"书架上共 {len(rows)} 个作品：")
        for line in _table(rows):
            out(line)

        if args.only_id:
            targets = {t.strip() for t in args.only_id.split(",") if t.strip()}
            picked = [w for w in rows if w["id"] in targets]
        else:
            picked = [w for w in rows if w["id"] not in keep]

        if not picked:
            out("\n没有要删的作品（保留列表已覆盖全部）")
            return 0

        out(f"\n将删除 {len(picked)} 个，保留 {len(rows) - len(picked)} 个"
            f"（保留 id：{sorted(keep) if not args.only_id else '未使用'}）")
        for w in picked:
            out(f"  - {w['id']} {w.get('title') or ''} "
                f"{w['nodes']} 节 / {w['words']} 字 / 实体 {w['ents']}")

        if not args.yes:
            out("\n（试运行：什么也没删。确认无误后加 --yes）")
            return 0

        maybe_backup(args)

        deleted = 0
        for w in picked:
            if await store.delete_work(w["id"]):
                deleted += 1
            else:
                out(f"  删除失败：{w['id']}")
        # 生成表达式里不能写 await，剩下的作品先取成列表再逐个统计
        left = await store.list_works(limit=200)
        with_graph = 0
        for w in left:
            if (await store.graph_stats(w["id"]))["entities"]:
                with_graph += 1
        out(f"\n已删除 {deleted} 个，书架剩 {len(left)} 个，其中 {with_graph} 个带图谱")
        orphans = await store.orphan_counts()
        if orphans:
            out(f"警告：删完还有孤儿行 {orphans}，跑 --sweep-orphans --yes 清掉")
    finally:
        await store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
