"""写作业务存储：作品 / 节点树 / 版本 / 素材 / 事实 / 术语 / 告警 / 生成记录。

与 ConversationStore 分开：一个是对话落库，一个是创作数据。
所有方法都用同一把锁串行化——单机自用，并发很低，正确性优先。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from loguru import logger

from app.studio import content_types
from app.studio.schema import BUILTIN_TEMPLATES, SCHEMA

# 单次列表查询的上限，防止误传巨大 limit 把内存打满
_MAX_LIMIT = 500

# work_id 上没有 REFERENCES works(id) 的表（这几张建得早，外键是后来补的）。
# 删作品时手动清，历史遗留的孤儿行由 sweep_orphans 兜底。
_NO_FK_TABLES = ("jobs", "runs", "index_units")

# 孤儿行判定：一张表一条 WHERE。条件要和 delete_work / delete_node / delete_source
# 各自的清理范围一一对应：删作品按 work_id 判就够，而删单节/单条素材时作品还活着，
# 只能按「指向的节点/素材还在不在」判。
_ORPHAN_WHERE = {
    "jobs": "work_id NOT IN (SELECT id FROM works)",
    "runs": "work_id NOT IN (SELECT id FROM works)",
    "index_units": (
        "work_id NOT IN (SELECT id FROM works) "
        "OR (ref_kind = 'node' AND ref_id NOT IN (SELECT id FROM nodes)) "
        "OR (ref_kind = 'source' AND ref_id NOT IN (SELECT id FROM sources))"
    ),
    "chunks_fts": (
        "chunk_id NOT IN (SELECT id FROM chunks) "
        "OR work_id NOT IN (SELECT id FROM works)"
    ),
    "summaries": "scope IN ('node', 'container') AND ref_id NOT IN (SELECT id FROM nodes)",
}

# 章号（ordinal）在库里不止 nodes 一处：图谱的时间线、事件顺序、伏笔超期判定各存了一份，
# 而且这些地方只存章号、不存节点 id，没得跟着 nodes 一起查。
# 重排时这些列必须跟着对调，否则「某人第 2 章死了」会留在第 3 章的位置上，
# 一致性检查照着章号比对，排一次序就把设定搞乱。
_ORDINAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entities", "first_ordinal"),
    ("entities", "status_ordinal"),
    ("relations", "since_ordinal"),
    ("relations", "until_ordinal"),
    ("relations", "evidence_ordinal"),
    ("events", "ordinal"),
    ("entity_states", "ordinal"),
    ("foreshadows", "planted_ordinal"),
    ("foreshadows", "planned_payoff_ordinal"),
    ("foreshadows", "resolved_ordinal"),
)


def _now() -> float:
    return time.time()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _count_words(text: str) -> int:
    """中文字数按字符粗算，英文按空白分词。写作场景够用。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = len([w for w in "".join(ch if not ("\u4e00" <= ch <= "\u9fff") else " " for ch in text).split() if w])
    return cjk + rest


class StudioStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        import asyncio

        self._lock = asyncio.Lock()

    # ==================== 生命周期 ====================

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()
        await self._seed_templates()

    async def _migrate(self) -> None:
        """给已有库补新列。

        `SCHEMA` 全是 `CREATE TABLE IF NOT EXISTS`，对新库够用、对老库一句都不改：
        表已经存在就直接跳过，后加上去的列根本不会出现。`summaries.ref_hash` 就是
        这种情况——缺它的话摘要没法判断「正文改过了」，改完稿子下游还在用旧记忆。

        回填很要紧：新列默认空串，而空串在我们的口径里等于「已过期」。
        不回填的话升级当天所有现存摘要都会被判过期，S3/S4 直接空掉，
        看起来就像「记忆全丢了」。所以历史行一律按当时的内容指纹补上。
        """
        assert self._db is not None
        cur = await self._db.execute("PRAGMA table_info(summaries)")
        cols = {r[1] for r in await cur.fetchall()}
        if "ref_hash" in cols:
            return
        await self._db.execute("ALTER TABLE summaries ADD COLUMN ref_hash TEXT DEFAULT ''")
        # 节摘要：拿节点自己的 content_hash；容器/全书摘要没有单一源文本，
        # 用一个非空占位表示「当年生成过、没理由认为它过期」。
        await self._db.execute(
            "UPDATE summaries SET ref_hash = "
            "COALESCE(NULLIF(ref_hash, ''), "
            "(SELECT n.content_hash FROM nodes n WHERE n.id = summaries.ref_id), '') "
            "WHERE scope = 'node'"
        )
        await self._db.execute(
            "UPDATE summaries SET ref_hash = 'legacy' "
            "WHERE scope IN ('container', 'work') AND ref_hash = ''"
        )
        await self._db.commit()
        logger.info("[studio] summaries 已补 ref_hash 列并回填历史指纹")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _seed_templates(self) -> None:
        """模板以 content-types.yaml 为准。

        内置模板（is_builtin=1）每次启动用 YAML 覆盖，这样改配置立即生效；
        用户自建模板（is_builtin=0）不动。YAML 缺失时回退到 schema 里的内置定义。
        """
        assert self._db is not None
        import json

        tpls = content_types.templates() or BUILTIN_TEMPLATES
        async with self._lock:
            for tpl in tpls:
                await self._db.execute(
                    "INSERT INTO templates(id, kind, subtype, name, outline_json, "
                    "style_prompt, checks_json, is_builtin, created_at) VALUES(?,?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "kind=excluded.kind, subtype=excluded.subtype, name=excluded.name, "
                    "outline_json=excluded.outline_json, style_prompt=excluded.style_prompt, "
                    "checks_json=excluded.checks_json "
                    "WHERE templates.is_builtin = 1",
                    (
                        tpl["id"],
                        tpl["kind"],
                        tpl.get("subtype", ""),
                        tpl["name"],
                        json.dumps(tpl["outline"], ensure_ascii=False),
                        tpl.get("style", ""),
                        json.dumps(tpl.get("checks", []), ensure_ascii=False),
                        _now(),
                    ),
                )
            await self._db.commit()

    # ==================== 模板 ====================

    async def sync_templates(self) -> None:
        """改完 content-types.yaml 后调一次，把内置模板重新同步进库。"""
        await self._seed_templates()

    async def list_templates(self, kind: str = "", subtype: str = "") -> list[dict[str, Any]]:
        assert self._db is not None
        sql = "SELECT * FROM templates WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if subtype:
            sql += " AND subtype = ?"
            params.append(subtype)
        sql += " ORDER BY is_builtin DESC, created_at ASC"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ==================== 作品 ====================

    async def create_work(
        self,
        *,
        kind: str,
        title: str,
        scale: str = "single",
        subtype: str = "",
        premise: str = "",
        template_id: Optional[str] = None,
        target_words: int = 0,
    ) -> dict[str, Any]:
        assert self._db is not None
        wid = _new_id("w_")
        now = _now()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO works(id, kind, scale, subtype, title, premise, status, template_id, "
                "target_words, style_json, meta_json, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,'active',?,?,'{}','{}',?,?)",
                (wid, kind, scale, subtype, title, premise, template_id, target_words, now, now),
            )
            await self._db.execute(
                "INSERT INTO index_state(work_id, updated_at) VALUES(?,?)", (wid, now)
            )
            await self._db.commit()
        return await self.get_work(wid)  # type: ignore[return-value]

    async def list_works(self, *, kind: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        assert self._db is not None
        sql = (
            "SELECT w.*, "
            "(SELECT COUNT(*) FROM nodes n WHERE n.work_id = w.id AND n.ordinal IS NOT NULL) AS node_count, "
            "(SELECT COALESCE(SUM(n.word_count),0) FROM nodes n WHERE n.work_id = w.id) AS words "
            "FROM works w WHERE 1=1"
        )
        params: list[Any] = []
        if kind:
            sql += " AND w.kind = ?"
            params.append(kind)
        if status:
            sql += " AND w.status = ?"
            params.append(status)
        sql += " ORDER BY w.updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, _MAX_LIMIT)))
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_work(self, work_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT w.*, "
                "(SELECT COUNT(*) FROM nodes n WHERE n.work_id = w.id AND n.ordinal IS NOT NULL) AS node_count, "
                "(SELECT COALESCE(SUM(n.word_count),0) FROM nodes n WHERE n.work_id = w.id) AS words "
                "FROM works w WHERE w.id = ?",
                (work_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_work(self, work_id: str, **fields: Any) -> bool:
        allowed = {"title", "premise", "status", "scale", "subtype", "template_id", "target_words",
                   "style_json", "meta_json"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [_now(), work_id]
        async with self._lock:
            cur = await self._db.execute(
                f"UPDATE works SET {sets}, updated_at = ? WHERE id = ?", params
            )
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_work(self, work_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            # _NO_FK_TABLES 与 chunks_fts 都没有外键兜底，必须手动清：
            # jobs 留着会让队列领到旧任务、连续报「作品不存在」；runs 留着会让成本与
            # 质量统计里混入不存在作品的分数；index_units 留着会让重建时误判
            # 「内容没变，跳过索引」；chunks_fts 是虚拟表，正文块被级联删掉之后
            # 倒排索引里的行还在。
            for table in _NO_FK_TABLES:
                await self._db.execute(f"DELETE FROM {table} WHERE work_id = ?", (work_id,))
            await self._db.execute("DELETE FROM chunks_fts WHERE work_id = ?", (work_id,))
            cur = await self._db.execute("DELETE FROM works WHERE id = ?", (work_id,))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def orphan_counts(self) -> dict[str, int]:
        """不受外键保护、且指向已消失的作品/节点/素材的行数。只返回非零项，全干净时为空。"""
        assert self._db is not None
        out: dict[str, int] = {}
        async with self._lock:
            for table, cond in _ORPHAN_WHERE.items():
                cur = await self._db.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {cond}")
                out[table] = int((await cur.fetchone())["c"])
        return {t: c for t, c in out.items() if c}

    async def sweep_orphans(self) -> dict[str, int]:
        """清掉孤儿行，返回每张表实际删了几条（同样只返回非零项）。

        存在的理由：`delete_work` / `delete_node` / `delete_source` 漏清这几张表是后来才修的，
        在那之前删掉的东西已经在库里留下了行（实测一次清理留下 runs 54 / index_units 58 /
        chunks_fts 63）。这些行大多不影响正确性——查询都按 work_id 过滤——但会让成本统计与
        搜索索引慢慢虚胖；`summaries` 那一类更烦：删掉的章节会以摘要的形式继续回到生成上下文。
        所以留个显式的清理入口，而不是让人手写 SQL。
        """
        assert self._db is not None
        deleted: dict[str, int] = {}
        async with self._lock:
            for table, cond in _ORPHAN_WHERE.items():
                cur = await self._db.execute(f"DELETE FROM {table} WHERE {cond}")
                deleted[table] = int(cur.rowcount or 0)
            await self._db.commit()
        return {t: c for t, c in deleted.items() if c}

    # ==================== 节点树 ====================

    async def _next_seq(self, work_id: str, parent_id: Optional[str]) -> int:
        """同级下一个序号：从 path 最后一段解析（单书节点数 < 10000，4 位足够）。"""
        assert self._db is not None
        if parent_id:
            cur = await self._db.execute(
                "SELECT path FROM nodes WHERE work_id = ? AND parent_id = ?", (work_id, parent_id)
            )
        else:
            cur = await self._db.execute(
                "SELECT path FROM nodes WHERE work_id = ? AND parent_id IS NULL", (work_id,)
            )
        rows = await cur.fetchall()
        seqs = []
        for r in rows:
            try:
                seqs.append(int(str(r[0]).rsplit(".", 1)[-1]))
            except ValueError:
                continue
        return (max(seqs) + 1) if seqs else 1

    async def create_node(
        self,
        *,
        work_id: str,
        kind: str,
        title: str,
        parent_id: Optional[str] = None,
        content: str = "",
        ordinal: Optional[int] = None,
    ) -> dict[str, Any]:
        assert self._db is not None
        nid = _new_id("n_")
        now = _now()

        async with self._lock:
            parent_path = ""
            depth = 0
            if parent_id:
                cur = await self._db.execute(
                    "SELECT path, depth FROM nodes WHERE id = ? AND work_id = ?", (parent_id, work_id)
                )
                row = await cur.fetchone()
                if not row:
                    raise ValueError(f"父节点不存在: {parent_id}")
                parent_path = row["path"] + "."
                depth = int(row["depth"]) + 1

            seq = await self._next_seq(work_id, parent_id)
            path = f"{parent_path}{seq:04d}"

            # 只有内容单元才进全序；容器（卷/篇）ordinal 为 NULL
            if ordinal is None and kind in ("chapter", "section"):
                cur = await self._db.execute(
                    "SELECT COALESCE(MAX(ordinal),0) AS m FROM nodes WHERE work_id = ?", (work_id,)
                )
                row = await cur.fetchone()
                ordinal = int(row["m"]) + 1

            await self._db.execute(
                "INSERT INTO nodes(id, work_id, parent_id, path, depth, ordinal, kind, title, content, "
                "status, word_count, content_hash, summary, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'draft',?,?,'',?,?)",
                (nid, work_id, parent_id, path, depth, ordinal, kind, title, content,
                 _count_words(content), _hash(content) if content else "", now, now),
            )
            await self._db.execute("UPDATE works SET updated_at = ? WHERE id = ?", (now, work_id))
            await self._db.commit()

        return await self.get_node(nid)  # type: ignore[return-value]

    async def list_tree(self, work_id: str) -> list[dict[str, Any]]:
        """整棵树的轻量视图（不带正文），按 path 排序，前端自行组装。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, parent_id, path, depth, ordinal, kind, title, status, word_count, "
                "updated_at FROM nodes WHERE work_id = ? ORDER BY path",
                (work_id,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_readable(self, work_id: str, *, only_empty: bool = False) -> list[dict[str, Any]]:
        """可读单元（有 ordinal 的节点）按全序返回，供流水线逐节生成用。"""
        assert self._db is not None
        sql = (
            "SELECT id, parent_id, path, depth, ordinal, kind, title, status, word_count, "
            "length(content) AS chars FROM nodes WHERE work_id = ? AND ordinal IS NOT NULL"
        )
        params: list[Any] = [work_id]
        if only_empty:
            sql += " AND (content IS NULL OR content = '')"
        sql += " ORDER BY ordinal"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_node(
        self, node_id: str, *, snapshot: bool = False,
        snapshot_source: str = "human", run_id: Optional[str] = None, **fields: Any,
    ) -> bool:
        """改节点。`snapshot=True` 时把改后的正文另存为一个版本。

        `snapshot_source` / `run_id` 是给「历史」页认人的：都是 `ai_draft` 生成的，
        还是自己手敲的，回滚前得先看得懂那一版是什么来路（`run_id` 再追到哪一次生成）。
        版本表里存的是**改后**的正文，所以最新版本与当前正文内容相同——
        回滚时靠这一点判断「当下这份要不要先补一版」，见 `restore_version`。
        """
        allowed = {"title", "content", "status", "summary"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        assert self._db is not None
        now = _now()

        async with self._lock:
            cur = await self._db.execute(
                "SELECT content, work_id FROM nodes WHERE id = ?", (node_id,)
            )
            row = await cur.fetchone()
            if not row:
                return False
            old = row["content"] or ""
            if "content" in updates:
                text = updates["content"] or ""
                updates["word_count"] = _count_words(text)
                updates["content_hash"] = _hash(text) if text else ""

            sets = ", ".join(f"{k} = ?" for k in updates)
            params = list(updates.values()) + [now, node_id]
            await self._db.execute(f"UPDATE nodes SET {sets}, updated_at = ? WHERE id = ?", params)

            # 正文真改了才让下游记忆跟着失效（改标题/状态不算）。
            # 节摘要不需要显式标：它的 ref_hash 与新 content_hash 自然对不上。
            # 但卷/讲与全书摘要是多节拼出来的，没任何一个字段能反映它旧了，
            # 只能在这一刻主动置空——不然改完第 7 章，S3 会继续拿着旧卷概要用下去。
            if "content" in updates and (updates["content"] or "") != old:
                await self._mark_stale_locked(row["work_id"], node_id)

            # 只在显式要求时留版本：自动保存每几秒一次，全存会把库撑爆
            if snapshot and "content" in updates and (updates["content"] or "") != old:
                await self._append_version_locked(
                    node_id, updates["content"] or "",
                    source=snapshot_source, run_id=run_id, now=now)
            await self._db.commit()
        return True

    async def _append_version_locked(
        self, node_id: str, content: str, *, source: str,
        run_id: Optional[str], now: float,
    ) -> int:
        """追加一个版本，返回它的 `version_no`。**调用方必须已持有 self._lock**。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT COALESCE(MAX(version_no),0) AS v FROM node_versions WHERE node_id = ?",
            (node_id,),
        )
        row = await cur.fetchone()
        vno = int(row["v"]) + 1
        await self._db.execute(
            "INSERT INTO node_versions(id, node_id, version_no, content, source, run_id, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (_new_id("v_"), node_id, vno, content, source, run_id, now),
        )
        return vno

    async def append_version(
        self, node_id: str, content: str, *, source: str = "human",
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """对外的「存一版」入口（回滚前保住当下草稿用的）。"""
        assert self._db is not None
        now = _now()
        async with self._lock:
            vno = await self._append_version_locked(
                node_id, content, source=source, run_id=run_id, now=now)
            await self._db.commit()
        return {"version_no": vno, "source": source, "chars": len(content), "created_at": now}

    async def _swap_ordinals_locked(
        self, work_id: str, table: str, col: str, a: int, b: int,
    ) -> None:
        """把某一列上的两个章号对调。

        **调用方必须已持有 self._lock**。

        不能直接写 `CASE col WHEN a THEN b WHEN b THEN a`：SQLite 逐行更新，
        把 2 改成 3 的那一行稍后又被 3 → 2 的条件命中；同一列上还带着
        `entity_states UNIQUE(entity_id, ordinal)`、`relations UNIQUE(src, rel, dst, since_ordinal)`
        这种约束，中间态会直接撞唯一索引。所以分两步：先整体取负
        （负值与任何真实章号都不冲突，彼此也不相同），再翻回目标值。
        章号为 0 或 NULL 的行不参与（`IN (?, ?)` 本身就把 NULL 排除在外）。
        """
        assert self._db is not None
        await self._db.execute(
            f"UPDATE {table} SET {col} = -{col} WHERE work_id = ? AND {col} IN (?, ?)",
            (work_id, a, b),
        )
        await self._db.execute(
            f"UPDATE {table} SET {col} = CASE {col} WHEN ? THEN ? WHEN ? THEN ? END "
            f"WHERE work_id = ? AND {col} IN (?, ?)",
            (-a, b, -b, a, work_id, -a, -b),
        )

    async def _repath_locked(self, work_id: str, prefix: str, new_prefix: str) -> None:
        """把一整棵子树的 path 前缀换掉（含节自己）。**调用方必须已持有 self._lock**。

        子节点的 path 是以父节点 path 为前缀存的，换了一个有子节点的节就必须连子树一起换，
        不然树上的层级会和 parent_id 说不上。

        谓词写成「等于前缀或以 `前缀.` 开头」而不是 `前缀%`：真拿 `0001.0002%` 去 LIKE，
        `0001.0002` 的兄弟 `0001.0002x` 也会被误伤（当前 path 定长 4 位撞不上，
        但没理由让这一句的正确性依赖那个补零假设）。
        """
        assert self._db is not None
        await self._db.execute(
            "UPDATE nodes SET path = ? || substr(path, ?) "
            "WHERE work_id = ? AND (path = ? OR path LIKE ?)",
            (new_prefix, len(prefix) + 1, work_id, prefix, f"{prefix}.%"),
        )

    async def move_node(self, node_id: str, direction: str) -> dict[str, Any]:
        """同层上下移：和紧邻的前/后兄弟互换位置。

        返回 `{moved, ordinal, swapped_with_title, reason}`。只在同一个 parent 里找邻居：
        跨卷搬动要重写 path/depth、还要决定卷里空了一节怎么补，那些边界靠猜意图是猜不准的。

        两节名下凡是跟章号有关的东西都得跟着对调：
        - `nodes` 的 `ordinal` 和 `path`。树上的顺序来自 path（`list_tree ORDER BY path`），
          生成顺序来自 ordinal（`list_readable ORDER BY ordinal`）——只换 ordinal
          会让树上的位置一动不动、而正文的先后已经变了，两边对不上。
        - `summaries.ref_ordinal` 与 `chunks.ordinal` 按节点 id 精确改。
        - `_ORDINAL_COLUMNS` 里那些只存章号的列按值对调。
        内容没动，所以索引台账与摘要的哈希戳都不重算（重算没有意义、白烧调用），
        只把 `index_state.dirty` 置 1。
        """
        if direction not in ("up", "down"):
            raise ValueError(f"方向只能是 up / down: {direction}")
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, work_id, parent_id, path, ordinal, title FROM nodes WHERE id = ?",
                (node_id,),
            )
            me = await cur.fetchone()
            if not me:
                return {"moved": False, "reason": "节点不存在"}
            mine = int(me["ordinal"] or 0)
            if mine <= 0:
                return {"moved": False, "reason": "容器节点不参与排序"}

            parent = me["parent_id"]
            args: list[Any] = [me["work_id"]]
            link = "parent_id = ?" if parent else "parent_id IS NULL"
            if parent:
                args.append(parent)
            # up 拿紧邻的前一个，down 拿紧邻的后一个
            op, order = ("<", "DESC") if direction == "up" else (">", "ASC")
            cur = await self._db.execute(
                f"SELECT id, ordinal, path, title FROM nodes "
                f"WHERE work_id = ? AND {link} AND ordinal > 0 AND ordinal {op} ? "
                f"ORDER BY ordinal {order} LIMIT 1",
                [*args, mine],
            )
            other = await cur.fetchone()
            if not other:
                return {"moved": False, "reason": "已到边界"}
            theirs = int(other["ordinal"])
            now = _now()

            # path 先换。中间那一步需要一个不与任何真实 path 冲突的临时前缀：
            # 真实 path 只有数字和点，所以拿 `~` 开顶，再把节点 id 里的 `_` 抹掉
            # （`_` 在 LIKE 里是单字符通配符，留着会让下一步误伤别的行）。
            tmp = f"~{me['id'].replace('_', '')}"
            await self._repath_locked(me["work_id"], me["path"], tmp)
            await self._repath_locked(me["work_id"], other["path"], me["path"])
            await self._repath_locked(me["work_id"], tmp, other["path"])

            await self._db.execute(
                "UPDATE nodes SET ordinal = CASE id WHEN ? THEN ? WHEN ? THEN ? END, "
                "updated_at = ? WHERE id IN (?, ?)",
                (me["id"], theirs, other["id"], mine, now, me["id"], other["id"]),
            )
            for nid, new_ord in ((me["id"], theirs), (other["id"], mine)):
                await self._db.execute(
                    "UPDATE summaries SET ref_ordinal = ? WHERE scope = 'node' AND ref_id = ?",
                    (new_ord, nid),
                )
                await self._db.execute(
                    "UPDATE chunks SET ordinal = ? WHERE node_id = ?", (new_ord, nid)
                )
            for table, col in _ORDINAL_COLUMNS:
                await self._swap_ordinals_locked(me["work_id"], table, col, mine, theirs)

            await self._db.execute(
                "UPDATE works SET updated_at = ? WHERE id = ?", (now, me["work_id"])
            )
            await self._db.execute(
                "INSERT INTO index_state(work_id, dirty, updated_at) VALUES(?,1,?) "
                "ON CONFLICT(work_id) DO UPDATE SET dirty = 1, updated_at = excluded.updated_at",
                (me["work_id"], now),
            )
            await self._db.commit()
        return {
            "moved": True, "direction": direction, "ordinal": theirs,
            "swapped_with_title": other["title"], "reason": "",
        }

    async def _drop_unit_rows(self, ref_kind: str, ref_ids: list[str]) -> None:
        """清掉一个节点/素材被删后不受外键保护的那些行。**调用方必须已持有 self._lock**。

        `chunks` 会随 nodes/sources 级联删掉，但倒排索引（`chunks_fts` 是虚拟表）与
        索引台账（`index_units` 的 work_id 没写外键）管不到，得抢在级联之前按 chunk_id 清——
        晚一步就连 chunk_id 都查不到了。台账留着会让重建时误判「内容没变，跳过索引」。
        `summaries` 只有指向节点的那些要跟着走（scope 为 node / container，存的是章节与卷的 id）：
        S4「最近节摘要」是按 work_id 全量取的，摘要不删的话，
        已删章节会以摘要的形式继续出现在每次生成的上下文里。
        """
        assert self._db is not None
        if not ref_ids:
            return
        link = "node_id" if ref_kind == "node" else "source_id"
        marks = ",".join("?" * len(ref_ids))
        await self._db.execute(
            f"DELETE FROM chunks_fts WHERE chunk_id IN "
            f"(SELECT id FROM chunks WHERE {link} IN ({marks}))", list(ref_ids))
        await self._db.execute(
            f"DELETE FROM index_units WHERE ref_kind = ? AND ref_id IN ({marks})",
            [ref_kind, *ref_ids])
        if ref_kind == "node":
            await self._db.execute(
                "DELETE FROM summaries WHERE scope IN ('node', 'container') "
                f"AND ref_id IN ({marks})", list(ref_ids))

    async def delete_node(self, node_id: str) -> bool:
        """删节点连同其整棵子树（靠 path 前缀，不依赖递归）。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT work_id, path FROM nodes WHERE id = ?", (node_id,))
            row = await cur.fetchone()
            if not row:
                return False
            where = "work_id = ? AND (id = ? OR path LIKE ?)"
            params = (row["work_id"], node_id, f"{row['path']}.%")
            # 子树 id 要和下面删节点的谓词完全一致，不然清掉的行和删掉的节点对不上
            cur = await self._db.execute(f"SELECT id FROM nodes WHERE {where}", params)
            await self._drop_unit_rows("node", [r["id"] for r in await cur.fetchall()])
            await self._db.execute(f"DELETE FROM nodes WHERE {where}", params)
            await self._db.commit()
        return True

    async def list_versions(self, node_id: str, limit: int = 30) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, version_no, source, run_id, created_at, length(content) AS chars "
                "FROM node_versions WHERE node_id = ? ORDER BY version_no DESC LIMIT ?",
                (node_id, max(1, min(limit, _MAX_LIMIT))),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_version(self, version_id: str) -> Optional[dict[str, Any]]:
        """读回一个版本的全文：列表接口只给字数，对比与回滚都要靠这里拿正文。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM node_versions WHERE id = ?", (version_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _newest_version_content(self, node_id: str) -> str:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT content FROM node_versions WHERE node_id = ? "
                "ORDER BY version_no DESC LIMIT 1", (node_id,))
            row = await cur.fetchone()
        return (row["content"] if row else "") or ""

    async def restore_version(self, node_id: str, version_id: str) -> Optional[dict[str, Any]]:
        """回滚到某个历史版本，返回更新后的节点；版本不属于这个节点时给 None。

        顺序不能反：先把「当下这份还没进版本表的正文」存一版，再写回目标内容。
        自动保存只写 nodes 不写版本，用户改到一半直接点回滚，不先补这一版
        就把改动当场吞了——那不是回滚，是删稿。
        已经存过的那份不重复塞：版本表只存不重复的状态，
        否则「历史」页里会连着好几条一模一样的行，反而看不出改了什么。
        写回走 `update_node`，所以正文一改，摘要失效传播自动跟上。
        """
        target = await self.get_version(version_id)
        if not target or target["node_id"] != node_id:
            return None
        node = await self.get_node(node_id)
        if not node:
            return None
        current = node.get("content") or ""
        if current == (target.get("content") or ""):
            return node
        if current and current != await self._newest_version_content(node_id):
            await self.append_version(node_id, current, source="human")
        await self.update_node(node_id, content=target["content"] or "")
        return await self.get_node(node_id)

    # ==================== 素材 / 事实 / 术语 ====================

    async def add_source(
        self, work_id: str, *, content: str, title: str = "", url: str = "",
        kind: str = "note", published_at: str = "",
    ) -> dict[str, Any]:
        assert self._db is not None
        sid = _new_id("s_")
        now = _now()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO sources(id, work_id, kind, title, url, content, published_at, fetched_at, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, work_id, kind, title, url, content, published_at, now, now),
            )
            await self._db.commit()
        return {"id": sid, "title": title, "url": url, "chars": len(content), "published_at": published_at}

    async def list_sources(self, work_id: str, limit: int = 100) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, kind, title, url, published_at, used, length(content) AS chars, created_at "
                "FROM sources WHERE work_id = ? ORDER BY created_at DESC LIMIT ?",
                (work_id, max(1, min(limit, _MAX_LIMIT))),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_source(self, source_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_source(self, source_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            # 素材和节点一样：chunks 会级联删掉，但 chunks_fts / index_units 不会
            await self._drop_unit_rows("source", [source_id])
            cur = await self._db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def add_fact(
        self, work_id: str, *, statement: str, source_id: Optional[str] = None,
        confidence: float = 1.0, valid_until: str = "",
    ) -> dict[str, Any]:
        assert self._db is not None
        fid = _new_id("f_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO facts(id, work_id, statement, source_id, confidence, valid_until, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (fid, work_id, statement, source_id, confidence, valid_until, _now()),
            )
            await self._db.commit()
        return {"id": fid, "statement": statement, "source_id": source_id, "valid_until": valid_until}

    async def list_facts(self, work_id: str, limit: int = 200) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT f.*, s.title AS source_title FROM facts f "
                "LEFT JOIN sources s ON s.id = f.source_id "
                "WHERE f.work_id = ? ORDER BY f.created_at DESC LIMIT ?",
                (work_id, max(1, min(limit, _MAX_LIMIT))),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_fact(self, fact_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def upsert_term(
        self, work_id: str, *, term: str, definition: str = "",
        preferred: str = "", forbidden: list[str] | None = None,
    ) -> dict[str, Any]:
        assert self._db is not None
        import json

        tid = _new_id("t_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO terms(id, work_id, term, definition, preferred, forbidden, created_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(work_id, term) DO UPDATE SET "
                "definition=excluded.definition, preferred=excluded.preferred, "
                "forbidden=excluded.forbidden",
                (tid, work_id, term, definition, preferred or term,
                 json.dumps(forbidden or [], ensure_ascii=False), _now()),
            )
            await self._db.commit()
            cur = await self._db.execute(
                "SELECT * FROM terms WHERE work_id = ? AND term = ?", (work_id, term)
            )
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def list_terms(self, work_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM terms WHERE work_id = ? ORDER BY created_at DESC", (work_id,)
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_term(self, term_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("DELETE FROM terms WHERE id = ?", (term_id,))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    # ==================== 告警 ====================

    async def add_issue(
        self, work_id: str, *, rule: str, message: str, node_id: Optional[str] = None,
        severity: str = "warn", evidence: str = "", location: str = "",
    ) -> dict[str, Any]:
        assert self._db is not None
        iid = _new_id("i_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO issues(id, work_id, node_id, rule, severity, message, evidence, location, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (iid, work_id, node_id, rule, severity, message, evidence, location, _now()),
            )
            await self._db.commit()
        return {"id": iid, "rule": rule, "severity": severity, "message": message}

    async def list_issues(self, work_id: str, *, node_id: str = "", only_open: bool = True,
                          limit: int = 100) -> list[dict[str, Any]]:
        assert self._db is not None
        sql = "SELECT * FROM issues WHERE work_id = ?"
        params: list[Any] = [work_id]
        if node_id:
            sql += " AND node_id = ?"
            params.append(node_id)
        if only_open:
            sql += " AND resolved = 0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, _MAX_LIMIT)))
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def resolve_issue(self, issue_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("UPDATE issues SET resolved = 1 WHERE id = ?", (issue_id,))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    # ==================== 索引（chunks / FTS5） ====================

    async def index_stamp(self, ref_kind: str, ref_id: str) -> Optional[str]:
        """已索引内容的哈希；没索引过返回 None。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT content_hash FROM index_units WHERE ref_kind = ? AND ref_id = ?",
                (ref_kind, ref_id),
            )
            row = await cur.fetchone()
        return row["content_hash"] if row else None

    async def set_index_stamp(
        self, *, ref_kind: str, ref_id: str, work_id: str, content_hash: str,
        chunk_count: int = 0,
    ) -> None:
        """记一笔「这个 ref 的某个派生结果已是由对应内容算出来的」。

        分块与图谱抽取共用这张台账，靠 ref_kind 区分（chunk / graph）：
        图谱重抽不必碰正文的块，正文重分块也不该让图谱失效。
        """
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "INSERT INTO index_units(ref_kind, ref_id, work_id, content_hash, chunk_count, "
                "updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(ref_kind, ref_id) DO UPDATE SET "
                "work_id=excluded.work_id, content_hash=excluded.content_hash, "
                "chunk_count=excluded.chunk_count, updated_at=excluded.updated_at",
                (ref_kind, ref_id, work_id, content_hash, int(chunk_count), _now()),
            )
            await self._db.commit()

    async def list_index_units(self, work_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT ref_kind, ref_id, content_hash, chunk_count, updated_at "
                "FROM index_units WHERE work_id = ?",
                (work_id,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def replace_chunks(
        self,
        *,
        work_id: str,
        ref_kind: str,
        ref_id: str,
        source_kind: str,
        ordinal: int,
        content_hash: str,
        texts: list[dict[str, Any]],
        node_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> int:
        """整段替换某个节点/素材的块与 FTS 行。

        `texts` 里每项 {seq, text, text_tok, start, end}。
        先删旧块再插新块，保证不会出现“旧版本还在被检索到”。
        """
        assert self._db is not None
        where, value = ("node_id", node_id) if node_id else ("source_id", source_id)
        if not value:
            raise ValueError("replace_chunks 需要 node_id 或 source_id")
        now = _now()
        async with self._lock:
            cur = await self._db.execute(
                f"SELECT id FROM chunks WHERE {where} = ?", (value,)
            )
            old = [r["id"] for r in await cur.fetchall()]
            for cid in old:
                await self._db.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (cid,))
            await self._db.execute(f"DELETE FROM chunks WHERE {where} = ?", (value,))

            for item in texts:
                cid = _new_id("c_")
                await self._db.execute(
                    "INSERT INTO chunks(id, work_id, source_kind, node_id, source_id, ordinal, "
                    "seq_in_unit, text, text_tok, char_start, char_end) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, work_id, source_kind, node_id, source_id, int(ordinal),
                     int(item.get("seq") or 0), item["text"], item["text_tok"],
                     int(item.get("start") or 0), int(item.get("end") or 0)),
                )
                await self._db.execute(
                    "INSERT INTO chunks_fts(chunk_id, work_id, text_tok) VALUES(?,?,?)",
                    (cid, work_id, item["text_tok"]),
                )

            await self._db.execute(
                "INSERT INTO index_units(ref_kind, ref_id, work_id, content_hash, chunk_count, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(ref_kind, ref_id) DO UPDATE SET "
                "work_id=excluded.work_id, content_hash=excluded.content_hash, "
                "chunk_count=excluded.chunk_count, updated_at=excluded.updated_at",
                (ref_kind, ref_id, work_id, content_hash, len(texts), now),
            )
            await self._db.commit()
        return len(texts)

    async def drop_index_unit(self, *, ref_kind: str, ref_id: str, node_id: Optional[str] = None,
                              source_id: Optional[str] = None) -> None:
        assert self._db is not None
        where, value = ("node_id", node_id) if node_id else ("source_id", source_id)
        if not value:
            return
        async with self._lock:
            cur = await self._db.execute(f"SELECT id FROM chunks WHERE {where} = ?", (value,))
            for r in await cur.fetchall():
                await self._db.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (r["id"],))
            await self._db.execute(f"DELETE FROM chunks WHERE {where} = ?", (value,))
            await self._db.execute(
                "DELETE FROM index_units WHERE ref_kind = ? AND ref_id = ?", (ref_kind, ref_id)
            )
            await self._db.commit()

    async def clear_chunks(self, work_id: str) -> None:
        """整书重建前清空，避免残留已删节点的块。"""
        assert self._db is not None
        async with self._lock:
            await self._db.execute("DELETE FROM chunks_fts WHERE work_id = ?", (work_id,))
            await self._db.execute("DELETE FROM chunks WHERE work_id = ?", (work_id,))
            await self._db.execute("DELETE FROM index_units WHERE work_id = ?", (work_id,))
            await self._db.commit()

    async def count_chunks(self, work_id: str, source_kind: str = "") -> int:
        assert self._db is not None
        sql = "SELECT COUNT(*) AS c FROM chunks WHERE work_id = ?"
        params: list[Any] = [work_id]
        if source_kind:
            sql += " AND source_kind = ?"
            params.append(source_kind)
        async with self._lock:
            cur = await self._db.execute(sql, params)
            row = await cur.fetchone()
        return int(row["c"])

    async def search_chunks(
        self, work_id: str, match_expr: str, *, kinds: tuple[str, ...] = ("body", "source"),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """FTS5 召回。bm25 越小越相关，这里取负值变成“越大越相关”。

        分两步查（先 FTS 拿 chunk_id + bm25，再取块内容）：
        FTS5 与普通表 join 时，优化器偶尔会把普通表放在外层，
        直接报 `unable to use function MATCH in the requested context`。
        """
        assert self._db is not None
        if not match_expr:
            return []
        cap = max(1, min(limit, 200))
        async with self._lock:
            cur = await self._db.execute(
                "SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts "
                "WHERE chunks_fts MATCH ? AND work_id = ? ORDER BY rank LIMIT ?",
                (match_expr, work_id, cap * 2),
            )
            hits = await cur.fetchall()
            if not hits:
                return []
            order = {r["chunk_id"]: float(r["rank"] or 0.0) for r in hits}
            marks = ",".join("?" for _ in hits)
            kms = ",".join("?" for _ in kinds)
            cur = await self._db.execute(
                "SELECT id, source_kind, node_id, source_id, ordinal, seq_in_unit, text, "
                f"char_start, char_end FROM chunks WHERE id IN ({marks}) AND source_kind IN ({kms})",
                [*order.keys(), *kinds],
            )
            rows = await cur.fetchall()
        out = [dict(r) for r in rows]
        for d in out:
            d["score"] = -order.get(d["id"], 0.0)
        # 保持 FTS 的排序（IN 查询不保证顺序）
        out.sort(key=lambda d: order.get(d["id"], 0.0))
        return out[:cap]

    async def get_chunk_source_titles(self, work_id: str) -> dict[str, str]:
        """chunk 出处展示用：node_id/source_id -> 标题。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, title FROM nodes WHERE work_id = ?", (work_id,)
            )
            titles = {r["id"]: r["title"] for r in await cur.fetchall()}
            cur = await self._db.execute(
                "SELECT id, title FROM sources WHERE work_id = ?", (work_id,)
            )
            titles.update({r["id"]: (r["title"] or "素材") for r in await cur.fetchall()})
        return titles

    async def touch_index_state(self, work_id: str, **fields: Any) -> None:
        allowed = {"last_chunk_ordinal", "last_graph_ordinal", "last_summary_ordinal", "dirty"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "INSERT INTO index_state(work_id, updated_at) VALUES(?,?) "
                "ON CONFLICT(work_id) DO NOTHING",
                (work_id, _now()),
            )
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                await self._db.execute(
                    f"UPDATE index_state SET {sets}, updated_at = ? WHERE work_id = ?",
                    [*updates.values(), _now(), work_id],
                )
            await self._db.commit()

    async def index_state(self, work_id: str) -> dict[str, Any]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM index_state WHERE work_id = ?", (work_id,))
            row = await cur.fetchone()
        return dict(row) if row else {}

    # ==================== 图谱（M3） ====================

    async def list_cards(self, work_id: str, kind: str = "") -> list[dict[str, Any]]:
        assert self._db is not None
        sql = "SELECT * FROM cards WHERE work_id = ?"
        params: list[Any] = [work_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY pinned DESC, kind, name"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def upsert_card(
        self, work_id: str, *, kind: str, name: str, aliases: Optional[list[str]] = None,
        summary: str = "", data_json: str = "{}", pinned: bool = True, card_id: str = "",
    ) -> dict[str, Any]:
        """作者手写的设定卡。pinned 的卡优先级高于抽取结果，抽取时会被强制注入 prompt。"""
        assert self._db is not None
        import json as _json

        now = _now()
        exist = None
        async with self._lock:
            if card_id:
                cur = await self._db.execute(
                    "SELECT * FROM cards WHERE id = ? AND work_id = ?", (card_id, work_id))
                exist = await cur.fetchone()
            if exist is None:
                cur = await self._db.execute(
                    "SELECT * FROM cards WHERE work_id = ? AND name = ?", (work_id, name))
                exist = await cur.fetchone()

            if exist:
                await self._db.execute(
                    "UPDATE cards SET kind = ?, name = ?, aliases = ?, summary = ?, "
                    "data_json = ?, pinned = ?, updated_at = ? WHERE id = ?",
                    (kind, name, _json.dumps(aliases or [], ensure_ascii=False), summary,
                     data_json, 1 if pinned else 0, now, exist["id"]),
                )
                cid = exist["id"]
            else:
                cid = _new_id("card_")
                await self._db.execute(
                    "INSERT INTO cards(id, work_id, kind, name, aliases, summary, data_json, "
                    "pinned, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (cid, work_id, kind, name, _json.dumps(aliases or [], ensure_ascii=False),
                     summary, data_json, 1 if pinned else 0, now),
                )
            await self._db.commit()
            cur = await self._db.execute("SELECT * FROM cards WHERE id = ?", (cid,))
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def delete_card(self, card_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def link_entity_card(self, entity_id: str, card_id: str) -> None:
        """把实体挂到作者手写的卡上，面板上就能看出「这个人是作者定的」。"""
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE entities SET card_id = ? WHERE id = ?", (card_id, entity_id))
            await self._db.commit()

    async def clear_graph(self, work_id: str) -> dict[str, int]:
        """清空一本书的图谱与抽取台账，给「抽乱了重来」用。设定卡保留。"""
        assert self._db is not None
        out = {"entities": 0, "relations": 0, "events": 0, "foreshadows": 0, "states": 0}
        async with self._lock:
            for key, sql in (
                ("relations", "DELETE FROM relations WHERE work_id = ?"),
                ("states", "DELETE FROM entity_states WHERE work_id = ?"),
                ("events", "DELETE FROM events WHERE work_id = ?"),
                ("foreshadows", "DELETE FROM foreshadows WHERE work_id = ?"),
                ("entities", "DELETE FROM entities WHERE work_id = ?"),
            ):
                cur = await self._db.execute(sql, (work_id,))
                out[key] = cur.rowcount or 0
            await self._db.execute(
                "DELETE FROM index_units WHERE work_id = ? AND ref_kind = 'graph'", (work_id,))
            await self._db.commit()
        return out

    async def get_entity(self, entity_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM entities WHERE id = ?", (entity_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_entities(self, work_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM entities WHERE work_id = ? ORDER BY kind, first_ordinal", (work_id,)
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_entity_by_canonical(self, work_id: str, canonical: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM entities WHERE work_id = ? AND canonical = ?", (work_id, canonical)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_entity(
        self, work_id: str, *, kind: str, name: str, canonical: str,
        aliases: Optional[list[str]] = None, first_ordinal: int = 0,
    ) -> dict[str, Any]:
        """按 canonical 对齐：已存在就补别名/纠正类型，不存在则新建。"""
        assert self._db is not None
        import json

        exist = await self.get_entity_by_canonical(work_id, canonical)
        if exist:
            merged = json.loads(exist.get("aliases") or "[]")
            for a in aliases or []:
                if a and a not in merged and a != exist["name"]:
                    merged.append(a)
            async with self._lock:
                await self._db.execute(
                    "UPDATE entities SET aliases = ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False), exist["id"]),
                )
                await self._db.commit()
            exist["aliases"] = json.dumps(merged, ensure_ascii=False)
            return exist

        eid = _new_id("e_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO entities(id, work_id, kind, name, canonical, aliases, first_ordinal, "
                "status, status_ordinal) VALUES(?,?,?,?,?,?,?,'alive',0)",
                (eid, work_id, kind or "character", name, canonical,
                 json.dumps(aliases or [], ensure_ascii=False), int(first_ordinal or 0)),
            )
            await self._db.commit()
        return {
            "id": eid, "work_id": work_id, "kind": kind or "character", "name": name,
            "canonical": canonical, "aliases": json.dumps(aliases or [], ensure_ascii=False),
            "first_ordinal": int(first_ordinal or 0), "status": "alive", "status_ordinal": 0,
        }

    async def set_entity_status(self, entity_id: str, status: str, ordinal: int) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE entities SET status = ?, status_ordinal = ? WHERE id = ? AND ? >= status_ordinal",
                (status, int(ordinal), entity_id, int(ordinal)),
            )
            await self._db.commit()

    async def list_relations(self, work_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT r.*, s.name AS src_name, d.name AS dst_name "
                "FROM relations r JOIN entities s ON s.id = r.src_id "
                "JOIN entities d ON d.id = r.dst_id WHERE r.work_id = ?",
                (work_id,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def upsert_relation(
        self, work_id: str, *, src_id: str, dst_id: str, rel_type: str,
        since_ordinal: int = 0, evidence_ordinal: Optional[int] = None,
    ) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "INSERT INTO relations(id, work_id, src_id, dst_id, rel_type, since_ordinal, "
                "confidence, evidence_ordinal) VALUES(?,?,?,?,?,?,1.0,?) "
                "ON CONFLICT(src_id, rel_type, dst_id, since_ordinal) DO UPDATE SET "
                "confidence = MIN(1.0, relations.confidence + 0.1)",
                (_new_id("r_"), work_id, src_id, dst_id, rel_type, int(since_ordinal),
                 evidence_ordinal),
            )
            await self._db.commit()

    async def add_event(
        self, work_id: str, *, ordinal: int, summary: str, participants: list[str],
        location_id: Optional[str] = None, story_time: str = "", importance: int = 1,
    ) -> str:
        assert self._db is not None
        import json

        eid = _new_id("ev_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO events(id, work_id, ordinal, summary, participants, location_id, "
                "story_time, importance) VALUES(?,?,?,?,?,?,?,?)",
                (eid, work_id, int(ordinal), summary[:400],
                 json.dumps(participants, ensure_ascii=False), location_id,
                 story_time[:80], int(importance or 1)),
            )
            await self._db.commit()
        return eid

    async def list_events(self, work_id: str, limit: int = 500) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM events WHERE work_id = ? ORDER BY ordinal, seq_in_unit LIMIT ?",
                (work_id, max(1, min(limit, _MAX_LIMIT))),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def latest_state(self, entity_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM entity_states WHERE entity_id = ? ORDER BY ordinal DESC LIMIT 1",
                (entity_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_states(self, work_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM entity_states WHERE work_id = ? ORDER BY ordinal", (work_id,)
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_state(self, work_id: str, entity_id: str, ordinal: int, state_json: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "INSERT INTO entity_states(id, work_id, entity_id, ordinal, state_json) "
                "VALUES(?,?,?,?,?) ON CONFLICT(entity_id, ordinal) DO UPDATE SET "
                "state_json = excluded.state_json",
                (_new_id("st_"), work_id, entity_id, int(ordinal), state_json),
            )
            await self._db.commit()

    async def clear_events(self, work_id: str, ordinal: int) -> int:
        """重抽某一节前清掉旧的 event，避免同一段剧情被记两遍。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "DELETE FROM events WHERE work_id = ? AND ordinal = ?", (work_id, int(ordinal)))
            await self._db.commit()
        return cur.rowcount or 0

    async def graph_stats(self, work_id: str) -> dict[str, Any]:
        assert self._db is not None
        out: dict[str, Any] = {"entities": 0, "relations": 0, "events": 0,
                               "foreshadows_open": 0, "cards": 0, "by_kind": {}}
        async with self._lock:
            for key, sql in (
                ("entities", "SELECT COUNT(*) AS c FROM entities WHERE work_id = ?"),
                ("relations", "SELECT COUNT(*) AS c FROM relations WHERE work_id = ?"),
                ("events", "SELECT COUNT(*) AS c FROM events WHERE work_id = ?"),
                ("cards", "SELECT COUNT(*) AS c FROM cards WHERE work_id = ?"),
                ("foreshadows_open",
                 "SELECT COUNT(*) AS c FROM foreshadows WHERE work_id = ? AND status = 'open'"),
            ):
                cur = await self._db.execute(sql, (work_id,))
                row = await cur.fetchone()
                out[key] = int(row["c"])
            cur = await self._db.execute(
                "SELECT kind, COUNT(*) AS c FROM entities WHERE work_id = ? GROUP BY kind", (work_id,))
            out["by_kind"] = {r["kind"]: int(r["c"]) for r in await cur.fetchall()}
        return out

    async def list_foreshadows(self, work_id: str, status: str = "") -> list[dict[str, Any]]:
        assert self._db is not None
        sql = "SELECT * FROM foreshadows WHERE work_id = ?"
        params: list[Any] = [work_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY planted_ordinal"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_foreshadow(
        self, work_id: str, *, planted_ordinal: int, content: str,
        importance: int = 1, planned_payoff_ordinal: Optional[int] = None,
    ) -> str:
        assert self._db is not None
        fid = _new_id("fs_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO foreshadows(id, work_id, planted_ordinal, planned_payoff_ordinal, "
                "content, status, importance) VALUES(?,?,?,?,?,'open',?)",
                (fid, work_id, int(planted_ordinal), planned_payoff_ordinal, content[:300],
                 int(importance or 1)),
            )
            await self._db.commit()
        return fid

    async def resolve_foreshadow(self, foreshadow_id: str, ordinal: int) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE foreshadows SET status = 'resolved', resolved_ordinal = ? WHERE id = ?",
                (int(ordinal), foreshadow_id),
            )
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def issue_exists(self, work_id: str, node_id: Optional[str], rule: str,
                           message: str) -> bool:
        assert self._db is not None
        sql = "SELECT 1 FROM issues WHERE work_id = ? AND rule = ? AND message = ? AND resolved = 0"
        params: list[Any] = [work_id, rule, message]
        if node_id:
            sql += " AND node_id = ?"
            params.append(node_id)
        async with self._lock:
            cur = await self._db.execute(sql + " LIMIT 1", params)
            row = await cur.fetchone()
        return row is not None

    async def clear_issues(self, work_id: str, node_id: Optional[str] = None) -> int:
        assert self._db is not None
        sql = "DELETE FROM issues WHERE work_id = ?"
        params: list[Any] = [work_id]
        if node_id:
            sql += " AND node_id = ?"
            params.append(node_id)
        async with self._lock:
            cur = await self._db.execute(sql, params)
            await self._db.commit()
        return cur.rowcount or 0

    # ==================== 摘要 ====================

    async def upsert_summary(
        self, work_id: str, *, scope: str, ref_id: str, content: str,
        ref_ordinal: int = 0, model: str = "", ref_hash: str = "",
    ) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "INSERT INTO summaries(id, work_id, scope, ref_id, ref_ordinal, content, "
                "ref_hash, model, created_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope, ref_id) DO UPDATE SET "
                "content=excluded.content, ref_ordinal=excluded.ref_ordinal, "
                "ref_hash=excluded.ref_hash, model=excluded.model, created_at=excluded.created_at",
                (_new_id("sm_"), work_id, scope, ref_id, int(ref_ordinal), content,
                 ref_hash, model, _now()),
            )
            await self._db.commit()

    # 这一句在两个查询里重复用：节摘要拿节点当下的 content_hash 比，
    # 容器/全书摘要只能看 ref_hash 是不是被置空过（置空就是失效标记）。
    _SUMMARY_FRESH_SQL = (
        "SELECT s.*, "
        "CASE WHEN s.scope = 'node' "
        "     THEN (s.ref_hash <> '' AND s.ref_hash = COALESCE(n.content_hash, '')) "
        "     ELSE (s.ref_hash <> '') END AS fresh "
        "FROM summaries s LEFT JOIN nodes n ON n.id = s.ref_id "
    )

    async def get_summary(self, scope: str, ref_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                self._SUMMARY_FRESH_SQL + "WHERE s.scope = ? AND s.ref_id = ?",
                (scope, ref_id),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_summaries(self, work_id: str, scope: str = "") -> list[dict[str, Any]]:
        assert self._db is not None
        sql = self._SUMMARY_FRESH_SQL + "WHERE s.work_id = ?"
        params: list[Any] = [work_id]
        if scope:
            sql += " AND s.scope = ?"
            params.append(scope)
        sql += " ORDER BY s.ref_ordinal"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_summary(self, scope: str, ref_id: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "DELETE FROM summaries WHERE scope = ? AND ref_id = ?", (scope, ref_id)
            )
            await self._db.commit()

    async def stale_summaries(self, work_id: str) -> list[dict[str, Any]]:
        """过期的那几份摘要（只返回非空的行，调用方拿 len 就是「待重算 N 节」）。"""
        rows = await self.list_summaries(work_id)
        return [r for r in rows if not r.get("fresh")]

    async def _ancestor_ids_locked(self, node_id: str) -> list[str]:
        """从父节点往上收集容器 id（深度很浅，写循环比递归 CTE 好读）。"""
        assert self._db is not None
        out: list[str] = []
        cur_id: Optional[str] = node_id
        for _ in range(8):  # 层级上限防环：写作树最深就卷/讲/章/节
            cur = await self._db.execute(
                "SELECT parent_id FROM nodes WHERE id = ?", (cur_id,)
            )
            row = await cur.fetchone()
            if not row or not row["parent_id"]:
                break
            cur_id = row["parent_id"]
            out.append(cur_id)
        return out

    async def _mark_stale_locked(self, work_id: str, node_id: str) -> None:
        """把这一节上游的卷/全书摘要标为过期。**调用方必须已持有 self._lock**。"""
        assert self._db is not None
        chain = await self._ancestor_ids_locked(node_id)
        if chain:
            marks = ",".join("?" * len(chain))
            await self._db.execute(
                f"UPDATE summaries SET ref_hash = '' "
                f"WHERE scope = 'container' AND ref_id IN ({marks})", chain)
        await self._db.execute(
            "UPDATE summaries SET ref_hash = '' WHERE scope = 'work' AND work_id = ?",
            (work_id,))

    async def mark_summaries_stale(self, work_id: str, node_id: str) -> None:
        """对外入口（脚本/接口用）：手动把某一节的上游摘要判过期。"""
        assert self._db is not None
        async with self._lock:
            await self._mark_stale_locked(work_id, node_id)
            await self._db.commit()

    # ==================== 任务队列（M4） ====================

    async def enqueue_job(
        self, *, work_id: str, node_id: Optional[str], task_type: str,
        payload_json: str = "{}", priority: int = 5, idem_key: str = "",
        max_attempts: int = 2, scheduled_at: Optional[float] = None,
    ) -> dict[str, Any]:
        """幂等入队。

        幂等判定看「业务键 + 未结束状态」，而不是 `idem_key` 列：
        列上有 UNIQUE 约束，一旦历史 done 记录把键占住，再入队就会报
        `UNIQUE constraint failed`（实测踩过）。所以业务键用于查重，
        `idem_key` 列存「业务键#毫秒」只用来保留追溯。
        """
        assert self._db is not None
        now = _now()
        async with self._lock:
            if idem_key:
                cur = await self._db.execute(
                    "SELECT * FROM jobs WHERE work_id = ? AND COALESCE(node_id,'') = ? "
                    "AND task_type = ? AND status IN ('queued','running') "
                    "ORDER BY created_at DESC LIMIT 1",
                    (work_id, node_id or "", task_type))
                exist = await cur.fetchone()
                if exist:
                    row = dict(exist)
                    row["deduped"] = True
                    return row

            jid = _new_id("j_")
            unique_key = f"{idem_key or task_type}#{int(now * 1000)}"
            await self._db.execute(
                "INSERT INTO jobs(id, work_id, node_id, task_type, payload_json, status, priority, "
                "attempts, max_attempts, idem_key, scheduled_at, created_at, updated_at) "
                "VALUES(?,?,?,?,?,'queued',?,0,?,?,?,?,?)",
                (jid, work_id, node_id, task_type, payload_json, int(priority),
                 int(max_attempts), unique_key, scheduled_at, now, now),
            )
            await self._db.commit()
            cur = await self._db.execute("SELECT * FROM jobs WHERE id = ?", (jid,))
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def claim_job(self) -> Optional[dict[str, Any]]:
        """领取一个可执行任务：优先级高的先跑，同优先级按创建时间。

        先用 UPDATE ... WHERE status='queued' 抢，再用 rowcount 确认；
        单进程里其实不会竞争，但不想依赖“只有一个消费者”这个前提。
        """
        assert self._db is not None
        now = _now()
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id FROM jobs WHERE status = 'queued' "
                "AND (scheduled_at IS NULL OR scheduled_at <= ?) "
                "ORDER BY priority DESC, created_at ASC, rowid ASC LIMIT 1", (now,))
            row = await cur.fetchone()
            if not row:
                return None
            jid = row["id"]
            cur = await self._db.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? "
                "WHERE id = ? AND status = 'queued'", (now, jid))
            if (cur.rowcount or 0) == 0:
                return None
            await self._db.commit()
            cur = await self._db.execute("SELECT * FROM jobs WHERE id = ?", (jid,))
            got = await cur.fetchone()
        return dict(got) if got else None

    async def finish_job(self, job_id: str, *, status: str = "done", error: str = "") -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE jobs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error[:500], _now(), job_id))
            await self._db.commit()

    async def requeue_job(
        self, job_id: str, *, delay: float = 0.0, error: str = "", permanent: bool = False,
        rebate: bool = False,
    ) -> None:
        """退避重试；permanent=True 直接标 failed（不重试）。

        rebate=True 会把 attempts 减回去：风控/登录中断不是任务本身的错，
        不能因为这个把重试次数耗光。
        """
        assert self._db is not None
        now = _now()
        async with self._lock:
            if permanent:
                await self._db.execute(
                    "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                    (error[:500], now, job_id))
            else:
                await self._db.execute(
                    "UPDATE jobs SET status = 'queued', last_error = ?, scheduled_at = ?, "
                    "updated_at = ?, attempts = MAX(0, attempts - ?) WHERE id = ?",
                    (error[:500], now + max(0.0, delay), now, 1 if rebate else 0, job_id))
            await self._db.commit()

    async def retry_job(self, job_id: str) -> bool:
        """人工重试：连 attempts 一起清零。

        自动重试要限次数（防死循环），但人主动点的那一下是新的机会，
        否则刚修完配置，任务却因为“已经试过两次”而永远停在 failed。
        """
        assert self._db is not None
        now = _now()
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE jobs SET status = 'queued', attempts = 0, last_error = '', "
                "scheduled_at = NULL, updated_at = ? WHERE id = ? AND status = 'failed'",
                (now, job_id))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def cancel_job(self, job_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? "
                "AND status IN ('queued','failed')", (_now(), job_id))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def list_jobs(self, *, work_id: str = "", status: str = "",
                        limit: int = 100) -> list[dict[str, Any]]:
        assert self._db is not None
        sql = "SELECT * FROM jobs WHERE 1 = 1"
        params: list[Any] = []
        if work_id:
            sql += " AND work_id = ?"
            params.append(work_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY priority DESC, created_at ASC, rowid ASC LIMIT ?"
        params.append(max(1, min(limit, _MAX_LIMIT)))
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def job_counts(self, work_id: str = "") -> dict[str, int]:
        assert self._db is not None
        sql = "SELECT status, COUNT(*) AS c FROM jobs"
        params: list[Any] = []
        if work_id:
            sql += " WHERE work_id = ?"
            params.append(work_id)
        sql += " GROUP BY status"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return {r["status"]: int(r["c"]) for r in rows}

    async def reset_running_jobs(self) -> int:
        """进程重启后把 running 退回 queued：

        上一轮进程死在半路时，这些 job 既不会再被领，也不会重试，
        不重置就永远卡在那里。attempts 保留，避免死循环重试。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (_now(),))
            await self._db.commit()
        return cur.rowcount or 0

    # ==================== 生成记录 ====================

    async def start_run(self, *, work_id: str, node_id: Optional[str], task_type: str,
                        model: str, provider: str, prompt_chars: int, prompt_hash: str = "",
                        variant: int = 0) -> str:
        assert self._db is not None
        rid = _new_id("r_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO runs(id, work_id, node_id, task_type, model, provider, prompt_chars, "
                "prompt_hash, variant, status, started_at) VALUES(?,?,?,?,?,?,?,?,?,'running',?)",
                (rid, work_id, node_id, task_type, model, provider, prompt_chars, prompt_hash,
                 int(variant), _now()),
            )
            await self._db.commit()
        return rid

    async def finish_run(self, run_id: str, *, status: str, output_chars: int = 0,
                         error: str = "", elapsed_ms: int = 0, score: Optional[float] = None) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE runs SET status=?, output_chars=?, error=?, elapsed_ms=?, score=?, finished_at=? "
                "WHERE id = ?",
                (status, output_chars, error[:500], elapsed_ms, score, _now(), run_id),
            )
            await self._db.commit()

    async def list_runs(self, work_id: str, limit: int = 50) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, node_id, task_type, model, provider, output_chars, elapsed_ms, status, error, "
                "variant, score, started_at FROM runs WHERE work_id = ? ORDER BY started_at DESC LIMIT ?",
                (work_id, max(1, min(limit, _MAX_LIMIT))),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ==================== 统计 ====================

    async def stats(self, work_id: str) -> dict[str, Any]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT COUNT(*) AS nodes, COALESCE(SUM(word_count),0) AS words FROM nodes "
                "WHERE work_id = ? AND ordinal IS NOT NULL",
                (work_id,),
            )
            n = await cur.fetchone()
            cur = await self._db.execute(
                "SELECT status, COUNT(*) AS c FROM nodes WHERE work_id = ? AND ordinal IS NOT NULL "
                "GROUP BY status",
                (work_id,),
            )
            by_status = {r["status"]: r["c"] for r in await cur.fetchall()}
            cur = await self._db.execute(
                "SELECT COUNT(*) AS c FROM sources WHERE work_id = ?", (work_id,)
            )
            s = await cur.fetchone()
            cur = await self._db.execute(
                "SELECT COUNT(*) AS c FROM facts WHERE work_id = ?", (work_id,)
            )
            f = await cur.fetchone()
            cur = await self._db.execute(
                "SELECT COUNT(*) AS c FROM issues WHERE work_id = ? AND resolved = 0", (work_id,)
            )
            i = await cur.fetchone()
        return {
            "nodes": int(n["nodes"]),
            "words": int(n["words"]),
            "by_status": by_status,
            "sources": int(s["c"]),
            "facts": int(f["c"]),
            "open_issues": int(i["c"]),
        }
