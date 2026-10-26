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

`--list` 与不带 `--yes` 的试运行**完全不碰数据库**（只读备份目录里的文件），
所以服务开着也能放心跑；真恢复才开连接。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def peek(store, name: str) -> Optional[dict]:
    """读一份备份的内容摘要给试运行用；读不懂或结构不对就返回 None。

    这里校验过的东西，后面 `restore_node_backup` 返回 None 时才可以说得出原因——
    否则「文件是坏的」与「那本书整本删了」都长成一个样子，脚本报的错就是谎话。
    """
    path = store.backup_dir("nodes") / name
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return None
    return data


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

    # 只有真要往库里写时才开连接：`init()` 会建表、跑迁移、拿 YAML 覆盖内置模板，
    # 全是要写库的动作。而这份脚本的典型用法恰恰是「人刚删错、服务还开着」时跑一行 --list，
    # 那种时候抢同一张库既没必要，也可能被 `database is locked` 挡回来。
    # 列举与试运行都只读备份目录里的文件，不碰数据库。
    store = StudioStore(DB_PATH)
    opened = bool(args.name and args.yes)
    if opened:
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
            rows = bundle["nodes"]
            chars = sum(len(n.get("content") or "") for n in rows if isinstance(n, dict))
            vers = sum(len(n.get("versions") or []) for n in rows if isinstance(n, dict))
            out(f"将恢复「{bundle.get('title') or '（没有标题）'}」：{len(rows)} 节 / {chars} 字 / "
                f"{vers} 个历史版本，作品 {bundle.get('work_id') or '（未知）'}")
            if not args.yes:
                out("\n（试运行：什么也没写。加 --yes 真恢复）")
                return 0
            r = await store.restore_node_backup(args.name)
            if r is None:
                # peek 已经确认过文件读得懂，所以这里唯一剩下的原因是：备份里那个 work_id
                # 在库里查不到了（整本书也删了）。别把它说成「文件坏了」，那是另一回事
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
        if opened:
            await store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
