"""找回删掉的章节：列出删除前的备份，按文件名把那一节恢复回树里。

用法：
    .venv\\Scripts\\python.exe -m scripts.restore_backup --list
    .venv\\Scripts\\python.exe -m scripts.restore_backup --name w_xxx__n_yyy__20250301_120000.json
    .venv\\Scripts\\python.exe -m scripts.restore_backup --name ... --yes

为什么要留这个脚本而不是只在界面上加个按钮：删掉的那一刻人是慌的，
能一行命令看清楚「库里还留着哪几份、每份里有几节多少字」比什么都实在。
恢复 UI 本轮不做，接口是 `GET /studio/backups` 与 `POST /studio/backups/{name}/restore`。

默认试运行，加 `--yes` 才真往库里写。恢复的口径（`store.restore_node_backup`）：
原父节点还在就挂回它下面，父节点没了就挂到作品根；`path` 重新排、`ordinal` 追加到末尾，
要挪回原来的位置用树上的上下移；历史版本一并写回 `node_versions`。

整库副本（`*.sqlite`）这里只列不改：那种找回是「把整个库盖回去」，
得先停服务再替换文件，在本脚本里顺手做等于偷偷回滚别人这段时间写的东西。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "storage" / "studio.sqlite"


def out(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def list_backups(store) -> int:
    nodes = store.list_node_backups()
    out(f"节点备份 {len(nodes)} 份（{store.backup_dir('nodes')}）：")
    if not nodes:
        out("  没有——删除操作会自动写这一份，说明还没删过东西，或者都被清理掉了")
    for r in nodes:
        out(f"  {r['name']:<62} {r['title'][:18]:<20} {r['kind']:<8} "
            f"{r['node_count']} 节 {r['bytes']:>8}B  {_fmt_time(r['created_at'])}")

    works = store.list_work_backups()
    out(f"\n整库副本 {len(works)} 份（{store.backup_dir('works')}）：")
    for r in works:
        out(f"  {r['name']:<58} {r['bytes'] / 1048576:.1f}MB  {_fmt_time(r['created_at'])}")
    if works:
        out("  这些只能整本盖回去：先停服务，替换 storage/studio.sqlite，再启动。本脚本不动它们")
    return 0


def peek(store, name: str) -> dict | None:
    """读一份备份的内容摘要给试运行用；读不懂就返回 None。"""
    path = store.backup_dir("nodes") / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出所有备份（默认动作）")
    ap.add_argument("--name", default="", help="要恢复的节点备份文件名")
    ap.add_argument("--yes", action="store_true", help="真正写库，不加只打印会恢复什么")
    args = ap.parse_args()

    if not DB_PATH.exists():
        out(f"FAIL: 找不到 {DB_PATH}")
        return 1

    from app.studio.store import StudioStore

    store = StudioStore(DB_PATH)
    await store.init()
    try:
        if args.name:
            if args.name.endswith(".sqlite"):
                out("FAIL: 整库副本不走这里。停服务后把 storage/backups/works/<那个文件> "
                    "盖到 storage/studio.sqlite 即可")
                return 1
            bundle = peek(store, args.name)
            if bundle is None:
                out(f"FAIL: 备份不存在或读不懂：{args.name}（--list 看有哪些）")
                return 1
            chars = sum(len(n.get("content") or "") for n in bundle["nodes"])
            vers = sum(len(n.get("versions") or []) for n in bundle["nodes"])
            out(f"将恢复「{bundle['title']}」：{len(bundle['nodes'])} 节 / {chars} 字 / "
                f"{vers} 个历史版本，作品 {bundle['work_id']}")
            if not args.yes:
                out("\n（试运行：什么也没写。加 --yes 真恢复）")
                return 0
            r = await store.restore_node_backup(args.name)
            if r is None:
                # 唯一还会失败的原因：那本书已经整本删了（备份里的 work_id 查不到）
                out("FAIL: 这本书已经整本删除，节点备份没法单独挂回来；只能走整库副本")
                return 1
            for item in r["restored"]:
                flag = "（原父节点已没，挂到了作品根）" if item["hung_on_root"] else ""
                out(f"  + {item['kind']:<8} {item['title'][:20]:<22} "
                    f"ordinal={item['ordinal']} 版本 {item['versions']}{flag}")
            out(f"\n已恢复，根节点 id：{r['root_id']}")
            out(f"备份里记的原章号是 {r['original_ordinal']}，现在排在末尾——"
                f"在树上用 ↑↓ 挪回去即可")
            return 0

        return list_backups(store)
    finally:
        await store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
