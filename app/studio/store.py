"""写作业务存储：作品 / 节点树 / 版本 / 素材 / 事实 / 术语 / 告警 / 生成记录。

与 ConversationStore 分开：一个是对话落库，一个是创作数据。
所有方法都用同一把锁串行化——单机自用，并发很低，正确性优先。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime
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
# publications 虽然写了 REFERENCES，但连接从没开 `PRAGMA foreign_keys`，
# 级联删除实际不生效——写在这张名单里才是真的会被清掉。
_NO_FK_TABLES = ("jobs", "runs", "index_units", "publications")

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
    "publications": "work_id NOT IN (SELECT id FROM works)",
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

# 删除在这一层没有撤销键：`node_versions ON DELETE CASCADE` 挂在 nodes 上，
# 删卷会连带抹掉该卷每一章的全部历史版本。所以删之前先落盘一份，按时间留住最近的：
# 节点备份是一份 JSON，整库副本是一份 .sqlite（单作品级精准恢复做不了，不那个意）。
BACKUP_NODE_KEEP = 20
BACKUP_WORK_KEEP = 5
# 每日快照留 7 份。库实测 1 MB 出头，七份不到 10 MB，换的是「库文件本身坏了也有退路」——
# 上面两份都是**删除那一刻**才写的，没删过的东西不会自己多一份副本。
BACKUP_SNAPSHOT_KEEP = 7
# 备份目录里什么算「一份备份」：列举与清理共用这一条口径。
# 写临时名留下的 `.part`、别人顺手丢进来的东西都不在名单里。
_BACKUP_GLOB: dict[str, str] = {
    "nodes": "*.json", "works": "*.sqlite", "snapshots": "*.sqlite",
}
# 快照的两本记账：今天是否已取过、上次取的时候库最后一次改动到几点。
# 这两个键由循环自己写，不进 `_SETTING_KEYS` 白名单——人不该手工改它。
SNAPSHOT_DAY_KEY = "snapshot_last_day"
SNAPSHOT_TS_KEY = "snapshot_last_change"


class BackupError(RuntimeError):
    """备份没写成。调用方必须先拒绝删除。"""


def _now() -> float:
    return time.time()


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _to_float(raw: str) -> float:
    """settings 里存的是字符串，读的人只想要一个数。

    读不出来的当 0（=「从没快照过」）而不是抛错：这一本账是记账用的，
    一个被人手工改坏的值不该让后台快照整轮不做。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _backup_stat(p: Path) -> Optional[tuple[float, int]]:
    """取备份文件的 (mtime, 字节数)。

    列举与清理只是给人看「还留着哪几份」，不该因为某个文件恰好在这次扫描中途被清掉
    就整条命令报错。
    """
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_mtime, st.st_size


def check_node_bundle(data: Any) -> str:
    """节点备份的结构校验：返回空串表示这份能恢复，否则给出「哪儿不对」。

    判据必须与 `restore_node_backup` 的拒绝条件同源，而且脚本也用它——
    `scripts/restore_backup.py` 要先替 store 把文件校验过，才敢说
    「restore 返回 None 时唯一剩下的原因是那本书整本没了」。两处各写一套、
    脚本这一套只要松一分，`nodes` 是空的 / 条目不是对象 / 没写 work_id 这类
    坏文件就会被报成「书没了」，那正是本轮要消灭的那类谎话。
    """
    if not isinstance(data, dict):        # 人手工改过的备份可能是数组或一个字符串
        return "备份顶层不是对象"
    rows = data.get("nodes") or []
    if not isinstance(rows, list):
        return "备份里的 nodes 不是列表"
    if not all(isinstance(r, dict) for r in rows):
        return "备份里的 nodes 有不是对象的条目"
    if not rows:
        return "备份里没有任何一个节点"
    if not data.get("work_id"):
        return "备份里没写 work_id"
    return ""


def _count_words(text: str) -> int:
    """中文字数按字符粗算，英文按空白分词。写作场景够用。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    rest = len([w for w in "".join(ch if not ("\u4e00" <= ch <= "\u9fff") else " " for ch in text).split() if w])
    return cjk + rest


# ==================== 作品包（整本导出 / 导入） ====================
# 包是给人拷走、几周后再导回来的，所以格式与版本都写进文件；
# 版本对不上就拒绝，不做「尽力而为」的兼容——半懂不懂地导入比拒绝更危险。
BUNDLE_FORMAT = "novel-studio/work"
LIBRARY_FORMAT = "novel-studio/library"
BUNDLE_VERSION = 1
# 版本来源白名单与路由侧 `_VERSION_SOURCES` 同一份口径
BUNDLE_SOURCES = {"human", "ai_draft", "ai_polish", "bulk_replace"}
# 一份手改坏的包不该把库撑爆：单本两万节已经远超实际长篇
_MAX_BUNDLE_NODES = 20000
_MAX_LIBRARY_WORKS = 200


def _bundle_text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _bundle_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bundle_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _map_participants(raw: Any, entity_map: dict[str, str]) -> str:
    """事件的参与人是实体 id 列表（JSON 字符串）；换 id 时不在名单里的直接丢。"""
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        got = []
    if not isinstance(got, list):
        got = []
    return json.dumps([entity_map[x] for x in got if x in entity_map], ensure_ascii=False)


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
        # 每个新列独立判：早先这里是「ref_hash 在就直接 return」，
        # 再加列时会被这句话挡在门外，等于新列永远补不上。
        cur = await self._db.execute("PRAGMA table_info(summaries)")
        cols = {r[1] for r in await cur.fetchall()}
        if "ref_hash" not in cols:
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

        cur = await self._db.execute("PRAGMA table_info(works)")
        wcols = {r[1] for r in await cur.fetchall()}
        if "digest" not in wcols:
            await self._db.execute("ALTER TABLE works ADD COLUMN digest TEXT DEFAULT ''")
            await self._db.commit()
            logger.info("[studio] works 已补 digest 列（公众号摘要）")
        if "channel" not in wcols:
            # 公众号从「文种」降级成「渠道」：老作品一并迁过去。
            # 不迁的话它们会突然变成「没有渠道」，而界面上看不出这一列曾经存在。
            await self._db.execute("ALTER TABLE works ADD COLUMN channel TEXT DEFAULT ''")
            await self._db.execute(
                "UPDATE works SET channel = 'wechat', subtype = 'general' "
                "WHERE kind = 'article' AND subtype = 'wechat'"
            )
            await self._db.commit()
            logger.info("[studio] works 已补 channel 列，并把公众号作品从文种迁到渠道")

        cur = await self._db.execute("PRAGMA table_info(templates)")
        tcols = {r[1] for r in await cur.fetchall()}
        # 逐个判，不用「有 channel 就全跳过」：再加列时那句话会变成永远跨不过的门
        new_cols = {
            "channel": "TEXT DEFAULT ''",
            "tags_json": "TEXT DEFAULT '[]'",
            "note": "TEXT DEFAULT ''",
            "is_default": "INTEGER DEFAULT 0",
            "pinned": "INTEGER DEFAULT 0",
            "source_work_id": "TEXT DEFAULT ''",
            "updated_at": "REAL",
        }
        missing = {c: ddl for c, ddl in new_cols.items() if c not in tcols}
        for col, ddl in missing.items():
            await self._db.execute(f"ALTER TABLE templates ADD COLUMN {col} {ddl}")
        if missing:
            await self._db.execute(
                "UPDATE templates SET channel = 'wechat', subtype = 'general' "
                "WHERE channel = '' AND subtype = 'wechat'"
            )
            await self._db.commit()
            logger.info(f"[studio] templates 已补 {len(missing)} 列（渠道/收藏/默认等）")

        cur = await self._db.execute("PRAGMA table_info(nodes)")
        ncols = {r[1] for r in await cur.fetchall()}
        if "plan_json" not in ncols:
            await self._db.execute("ALTER TABLE nodes ADD COLUMN plan_json TEXT DEFAULT '{}'")
            # 老库里「本节计划」只活在 works.meta_json.outline。把它搬进节点，
            # 右栏「计划」页打开时才有内容；搬不动的（没有 prompt/target）就留空。
            cur = await self._db.execute("SELECT id, meta_json FROM works")
            works = await cur.fetchall()
            for work in works:
                try:
                    meta = json.loads(work["meta_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                outline = meta.get("outline") if isinstance(meta, dict) else None
                if not isinstance(outline, dict):
                    continue
                for node_id, info in outline.items():
                    if not isinstance(info, dict):
                        continue
                    try:
                        target = int(info.get("target_words") or 0)
                    except (TypeError, ValueError):
                        target = 0
                    plan = {
                        "prompt": str(info.get("prompt") or ""),
                        "target_words": target,
                    }
                    if not plan["prompt"] and not plan["target_words"]:
                        continue
                    await self._db.execute(
                        "UPDATE nodes SET plan_json = ? WHERE id = ? AND work_id = ?",
                        (json.dumps(plan, ensure_ascii=False), node_id, work["id"]),
                    )
            await self._db.commit()
            logger.info("[studio] nodes 已补 plan_json 列，并从 meta_json.outline 回填")

        cur = await self._db.execute("PRAGMA table_info(issues)")
        icols = {r[1] for r in await cur.fetchall()}
        if "resolution_note" not in icols:
            await self._db.execute(
                "ALTER TABLE issues ADD COLUMN resolution_note TEXT DEFAULT ''")
            await self._db.commit()
            logger.info("[studio] issues 已补 resolution_note 列（忽略原因）")

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
                # is_default / pinned 不进 INSERT：那是用户的选择，升级不能把它抹掉。
                await self._db.execute(
                    "INSERT INTO templates(id, kind, subtype, channel, name, outline_json, "
                    "style_prompt, checks_json, tags_json, note, is_builtin, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "kind=excluded.kind, subtype=excluded.subtype, channel=excluded.channel, "
                    "name=excluded.name, outline_json=excluded.outline_json, "
                    "style_prompt=excluded.style_prompt, checks_json=excluded.checks_json, "
                    "tags_json=excluded.tags_json, note=excluded.note, updated_at=excluded.updated_at "
                    "WHERE templates.is_builtin = 1",
                    (
                        tpl["id"],
                        tpl["kind"],
                        tpl.get("subtype", ""),
                        tpl.get("channel", ""),
                        tpl["name"],
                        json.dumps(tpl["outline"], ensure_ascii=False),
                        tpl.get("style", ""),
                        json.dumps(tpl.get("checks", []), ensure_ascii=False),
                        json.dumps(tpl.get("tags", []), ensure_ascii=False),
                        tpl.get("note", ""),
                        _now(),
                        _now(),
                    ),
                )
            await self._db.commit()

    # ==================== 模板 ====================

    async def sync_templates(self) -> None:
        """改完 content-types.yaml 后调一次，把内置模板重新同步进库。"""
        await self._seed_templates()

    async def list_templates(self, kind: str = "", subtype: str = "",
                             channel: str = "", q: str = "") -> list[dict[str, Any]]:
        """模板库列表。

        `use_count` 一起给前端：内置模板只能克隆，用得多不多决定它排多前。
        排序是「收藏 → 用得多的 → 内置优先 → 最近改过的」——常用模板应该
        一眼就能点到，而不是被一堆内置模板压在后面。
        """
        assert self._db is not None
        sql = (
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM works w WHERE w.template_id = t.id) AS use_count "
            "FROM templates t WHERE 1=1"
        )
        params: list[Any] = []
        if kind:
            sql += " AND t.kind = ?"
            params.append(kind)
        if subtype:
            sql += " AND t.subtype = ?"
            params.append(subtype)
        if channel:
            sql += " AND t.channel = ?"
            params.append(channel)
        if q:
            sql += " AND t.name LIKE ?"
            params.append(f"%{q}%")
        sql += " ORDER BY t.pinned DESC, use_count DESC, t.is_builtin DESC, t.updated_at DESC"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_template(self, template_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT t.*, "
                "(SELECT COUNT(*) FROM works w WHERE w.template_id = t.id) AS use_count "
                "FROM templates t WHERE t.id = ?",
                (template_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def create_template(
        self,
        *,
        kind: str,
        name: str,
        subtype: str = "",
        channel: str = "",
        outline: Optional[list[dict[str, Any]]] = None,
        style_prompt: str = "",
        checks: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        note: str = "",
        source_work_id: str = "",
        is_builtin: bool = False,
        template_id: str = "",
    ) -> dict[str, Any]:
        """新建模板。用户自建一律 is_builtin=0；is_builtin 只给导入用。"""
        assert self._db is not None
        tid = template_id or _new_id("tpl_u_")
        now = _now()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO templates(id, kind, subtype, channel, name, outline_json, style_prompt, "
                "checks_json, tags_json, note, source_work_id, is_builtin, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid, kind, subtype, channel, name,
                    json.dumps(outline or [], ensure_ascii=False),
                    style_prompt,
                    json.dumps(checks or [], ensure_ascii=False),
                    json.dumps(tags or [], ensure_ascii=False),
                    note, source_work_id, 1 if is_builtin else 0, now, now,
                ),
            )
            await self._db.commit()
        return await self.get_template(tid)  # type: ignore[return-value]

    async def update_template(self, template_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        """改模板。outline / tags / checks 传原始对象，这里统一序列化。"""
        assert self._db is not None
        mapping = {"tags": "tags_json", "checks": "checks_json", "outline": "outline_json"}
        allowed = {"kind", "subtype", "channel", "name", "style_prompt", "note",
                   "tags", "checks", "outline"}
        updates: dict[str, Any] = {}
        for key, val in fields.items():
            if key not in allowed or val is None:
                continue
            col = mapping.get(key, key)
            updates[col] = json.dumps(val, ensure_ascii=False) if col.endswith("_json") else val
        if not updates:
            return await self.get_template(template_id)
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self._lock:
            cur = await self._db.execute(
                f"UPDATE templates SET {sets} WHERE id = ?",
                list(updates.values()) + [template_id],
            )
            await self._db.commit()
        if (cur.rowcount or 0) <= 0:
            return None
        return await self.get_template(template_id)

    async def delete_template(self, template_id: str) -> dict[str, Any]:
        """删模板并解绑引用它的作品。

        先解绑再删：外键虽然是 ON DELETE SET NULL，但那样作品会「悄悄」
        变回无模板状态，界面上看不出是谁动的手。这里数出解绑了几本回给前端。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE works SET template_id = NULL, updated_at = ? WHERE template_id = ?",
                (_now(), template_id),
            )
            unbound = int(cur.rowcount or 0)
            cur = await self._db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
            deleted = int(cur.rowcount or 0)
            await self._db.commit()
        return {"deleted": deleted, "unbound": unbound}

    async def set_default_template(self, template_id: str) -> bool:
        """把某个模板设成它那组 (kind, subtype, channel) 的默认。

        同组只能有一个默认：否则 pick_template 的命中结果取决于插入顺序，
        同一类文章今天用 A 明天用 B，却看不出为什么。
        """
        assert self._db is not None
        tpl = await self.get_template(template_id)
        if not tpl:
            return False
        async with self._lock:
            await self._db.execute(
                "UPDATE templates SET is_default = 0 "
                "WHERE kind = ? AND subtype = ? AND channel = ? AND id != ?",
                (tpl["kind"], tpl.get("subtype") or "", tpl.get("channel") or "", template_id),
            )
            await self._db.execute(
                "UPDATE templates SET is_default = 1, updated_at = ? WHERE id = ?",
                (_now(), template_id),
            )
            await self._db.commit()
        return True

    async def toggle_template_pin(self, template_id: str,
                                  pinned: Optional[bool] = None) -> Optional[dict[str, Any]]:
        """收藏 / 取消收藏。不传 pinned 就是取反。"""
        tpl = await self.get_template(template_id)
        if not tpl:
            return None
        want = (not bool(tpl.get("pinned"))) if pinned is None else bool(pinned)
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "UPDATE templates SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if want else 0, _now(), template_id),
            )
            await self._db.commit()
        return await self.get_template(template_id)

    async def pick_template(self, kind: str, subtype: str = "",
                            channel: str = "") -> Optional[dict[str, Any]]:
        """按优先级挑一个模板。

        1) kind+subtype+channel 的默认   2) kind+subtype 的默认
        3) kind+channel 的默认         4) YAML 里 subtype 声明的出厂默认
        5) 该 kind 下任意一个            6) 空

        为什么要落到「任意一个」：新建作品时如果连一个模板都没有，单篇写作
        会退化成完全没有骨架；宁可给一个同类的，让人进去再换。
        """
        assert self._db is not None
        chains: list[tuple[str, tuple[Any, ...]]] = [
            ("kind = ? AND subtype = ? AND channel = ? AND is_default = 1", (kind, subtype, channel)),
            ("kind = ? AND subtype = ? AND is_default = 1", (kind, subtype)),
        ]
        if channel:
            # channel 为空时这一级与上一级等价，不白查一次
            chains.append(("kind = ? AND channel = ? AND is_default = 1", (kind, channel)))
        for cond, params in chains:
            async with self._lock:
                cur = await self._db.execute(
                    f"SELECT * FROM templates WHERE {cond} "
                    "ORDER BY pinned DESC, updated_at DESC LIMIT 1",
                    params,
                )
                row = await cur.fetchone()
            if row:
                return dict(row)
        tid = content_types.template_id_for(kind, "", subtype)
        if tid:
            got = await self.get_template(tid)
            if got:
                return got
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM templates WHERE kind = ? "
                "ORDER BY is_builtin DESC, pinned DESC, updated_at DESC LIMIT 1",
                (kind,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    # ==================== 作品 ====================

    async def create_work(
        self,
        *,
        kind: str,
        title: str,
        scale: str = "single",
        subtype: str = "",
        channel: str = "",
        premise: str = "",
        template_id: Optional[str] = None,
        target_words: int = 0,
    ) -> dict[str, Any]:
        assert self._db is not None
        wid = _new_id("w_")
        now = _now()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO works(id, kind, scale, subtype, channel, title, premise, status, template_id, "
                "target_words, style_json, meta_json, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,'active',?,?,'{}','{}',?,?)",
                (wid, kind, scale, subtype, channel, title, premise, template_id, target_words, now, now),
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
        allowed = {"title", "premise", "digest", "status", "scale", "subtype", "channel", "template_id",
                   "target_words", "style_json", "meta_json"}
        # template_id 是指向 templates 的外键：前端「不套模板」发的是空串，
        # 库里只能落 NULL（空串不是任何模板的 id，直接写会撞外键）。
        # 它是唯一允许 None 的列：其余列传 None 在路由层的 exclude_none
        # 就已经滤掉了，能进到这里的 None 只可能是这次显式清空。
        if "template_id" in fields and not str(fields["template_id"] or "").strip():
            fields["template_id"] = None
        updates = {k: v for k, v in fields.items()
                   if k in allowed and (v is not None or k == "template_id")}
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

    async def delete_work(self, work_id: str, *, backup: bool = True) -> bool:
        """删作品。**先取一份一致快照的整库副本，再动手**。本项无回滚入口，找回靠
        `scripts/restore_backup.py` 或直接把副本盖回去。

        为什么不做「单作品级备份」：一个作品的数据摊在 12 张表里（还含图谱、索引、
        生成记录），拼一份能准确回填的子集是个比 `delete_work` 本身更大的活，
        而且写错就是「看起来能恢复、恢复出来是半本」。整库副本不体面但真能回去。
        """
        assert self._db is not None
        async with self._lock:
            if backup:
                # 副本要在锁内做：它要的是「按下快门那一刻库是完整的」，
                # 锁外做等于跟后台队列的写事务抢同一张表，见 `_copy_db_backup`
                await self._copy_db_backup(f"work-{work_id}")
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
            # TRIM 而不是只比空串：正文只有一堆空白的节，batch 侧的 Python strip
            # 会当它空、这里也得当它空，否则两边选节数对不上（审查 M1）
            sql += " AND (content IS NULL OR TRIM(content) = '')"
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
        allowed = {"title", "content", "status", "summary", "plan_json"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        assert self._db is not None
        now = _now()

        async with self._lock:
            cur = await self._db.execute(
                "SELECT content, work_id, word_count FROM nodes WHERE id = ?", (node_id,)
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
                # 手写才计入「今天写了多少字」：AI 采用的正文走 ai_*，夜间跑批不该刷高统计
                if snapshot_source == "human":
                    await self._log_writes_locked(
                        now, int(updates["word_count"]) - int(row["word_count"] or 0))

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

    async def preserve_current_version(
        self, node_id: str, content: str, *, source: str = "human",
    ) -> bool:
        """把「还没进版本表的那一版」存下来；存过了就什么都不做，返回有没有存。

        判据与 `restore_version` 用的是同一条（最新版本已等于当前正文就不重复补）：
        两处各写一套的话，会出现一边补了重复行、另一边没补。版本表只存不重复的状态，
        不然「历史」页里连着好几条一模一样的行，反而看不出改了什么。

        动用户的稿子之前都得先过这一刀（批量替换就是）：自动保存不写版本，
        不补这一步，改之前的那一版在这个世界上就不存在了。
        """
        if not content or content == await self._newest_version_content(node_id):
            return False
        await self.append_version(node_id, content, source=source)
        return True

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
            if me["ordinal"] is None:
                # 容器（卷 / 篇）也允许上下移：交换的是整棵子树的 path 前缀，
                # 再按新的树序重排全书章号。只换 ordinal 不换 path 的话，
                # 树是按 path 排的，卷会一动不动。
                parent = me["parent_id"]
                args: list[Any] = [me["work_id"]]
                link = "parent_id = ?" if parent else "parent_id IS NULL"
                if parent:
                    args.append(parent)
                op, order = ("<", "DESC") if direction == "up" else (">", "ASC")
                cur = await self._db.execute(
                    f"SELECT id, path, title FROM nodes "
                    f"WHERE work_id = ? AND {link} AND ordinal IS NULL AND path {op} ? "
                    f"ORDER BY path {order} LIMIT 1",
                    [*args, me["path"]],
                )
                other = await cur.fetchone()
                if not other:
                    return {"moved": False, "reason": "已到边界"}
                now = _now()
                tmp = f"~{me['id'].replace('_', '')}"
                await self._repath_locked(me["work_id"], me["path"], tmp)
                await self._repath_locked(me["work_id"], other["path"], me["path"])
                await self._repath_locked(me["work_id"], tmp, other["path"])
                renumbered = await self._renumber_locked(me["work_id"])
                await self._db.execute(
                    "UPDATE works SET updated_at = ? WHERE id = ?", (now, me["work_id"])
                )
                await self._mark_index_dirty_locked(me["work_id"])
                await self._db.commit()
                return {
                    "moved": True, "direction": direction, "ordinal": None,
                    "swapped_with_title": other["title"], "renumbered": renumbered,
                }
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
            await self._mark_index_dirty_locked(me["work_id"])
            await self._db.commit()
        return {
            "moved": True, "direction": direction, "ordinal": theirs,
            "swapped_with_title": other["title"], "reason": "",
        }

    async def _renumber_locked(self, work_id: str) -> bool:
        """按 path 把全书章号重排成 1..N，返回是否真的改过。**调用方必须已持有 self._lock**。

        搬一节之后树上的位置变了，但章号还是旧的：`list_readable` 按 ordinal 排出生成
        顺序，一致性检查又按章号判伏笔超期与「死后出场」——号不跟着树走，结论就会错位。

        章号在库里不止 nodes 一处（`_ORDINAL_COLUMNS` 那十列），按值映射时不能逐列直接写：
        中间态会撞 `entity_states UNIQUE(entity_id, ordinal)` 与
        `relations UNIQUE(src_id, rel_type, dst_id, since_ordinal)`。所以先整体取负
        （负值不与任何真实章号冲突），再翻成新号；不在本次映射里的值（比如伏笔计划在
        尚未写到的第 100 章回收）原样留着——重排只对现存节点负责。
        """
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, ordinal FROM nodes WHERE work_id = ? AND ordinal IS NOT NULL ORDER BY path",
            (work_id,),
        )
        rows = await cur.fetchall()
        pairs = [(r["id"], int(r["ordinal"]), i) for i, r in enumerate(rows, 1)]
        if all(old == new for _, old, new in pairs):
            return False

        # nodes.ordinal 没有唯一约束，直接写；摘要与分块按节点 id 精确改
        for nid, _old, new in pairs:
            await self._db.execute("UPDATE nodes SET ordinal = ? WHERE id = ?", (new, nid))
            await self._db.execute(
                "UPDATE summaries SET ref_ordinal = ? WHERE scope = 'node' AND ref_id = ?",
                (new, nid),
            )
            await self._db.execute("UPDATE chunks SET ordinal = ? WHERE node_id = ?", (new, nid))

        mapping = {old: new for _, old, new in pairs}
        old_ords = sorted(mapping)
        marks = ",".join("?" * len(old_ords))
        for table, col in _ORDINAL_COLUMNS:
            await self._db.execute(
                f"UPDATE {table} SET {col} = -{col} WHERE work_id = ? AND {col} IN ({marks})",
                [work_id, *old_ords],
            )
        for table, col in _ORDINAL_COLUMNS:
            for old, new in mapping.items():
                await self._db.execute(
                    f"UPDATE {table} SET {col} = ? WHERE work_id = ? AND {col} = ?",
                    (new, work_id, -old),
                )
        return True

    async def reparent_node(
        self, node_id: str, parent_id: Optional[str], before_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """把一节挂到另一卷/章末尾，或插到某个兄弟节点之前。

        `before_id` 非空时优先于 `parent_id`：落点由那个兄弟的 parent 决定，
        用于拖拽到「某一行之前」。只搬有 ordinal 的叶子（章/节）；容器走
        `move_node` 的上下移。搬完按 path 重排全书章号（`_renumber_locked`），
        并标脏索引台账、刷新作品时间。

        返回 `{moved, ordinal, parent_title, renumbered}`；插到某节之前时
        多一个 `before_title`。已经在目标位置且全书序号连续时什么都不改。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, work_id, parent_id, path, depth, ordinal, title FROM nodes WHERE id = ?",
                (node_id,),
            )
            me = await cur.fetchone()
            if not me:
                return None
            if me["ordinal"] is None:
                raise ValueError("容器节点不能拖动：这里只搬章与节")
            work_id = me["work_id"]

            before = None
            if before_id:
                if before_id == node_id:
                    raise ValueError("不能插到自己前面")
                cur = await self._db.execute(
                    "SELECT id, work_id, parent_id, path, depth, title FROM nodes WHERE id = ?",
                    (before_id,),
                )
                before = await cur.fetchone()
                if not before:
                    return None
                if before["work_id"] != work_id:
                    raise ValueError("不能跨作品拖动")
                if str(before["path"]).startswith(f"{me['path']}."):
                    raise ValueError("不能把节点拖进它自己的子树")
                parent_id = before["parent_id"]

            target = None
            if parent_id:
                if parent_id == node_id:
                    raise ValueError("不能把节点拖进它自己")
                cur = await self._db.execute(
                    "SELECT id, work_id, path, depth, ordinal, title FROM nodes WHERE id = ?",
                    (parent_id,),
                )
                target = await cur.fetchone()
                if not target:
                    return None
                if target["work_id"] != work_id:
                    raise ValueError("不能跨作品拖动")
                if target["ordinal"] is not None:
                    # 章节带节时也算容器；空章仍拒绝，避免把章塞进另一个章里
                    cur = await self._db.execute(
                        "SELECT 1 FROM nodes WHERE parent_id = ? LIMIT 1", (parent_id,))
                    if not await cur.fetchone():
                        raise ValueError("目标不是容器：只能落到卷/篇或顶层")
                # 防御：叶子本来没有子树，真有人手工塞出环时别把 path 撞进自家分支
                if str(target["path"]).startswith(f"{me['path']}."):
                    raise ValueError("不能把节点拖进它自己的子树")

            # 同一父节点下已经紧挨在 before 前面：不动
            if before and (me["parent_id"] or None) == (parent_id or None):
                link = "parent_id = ?" if parent_id else "parent_id IS NULL"
                args: list[Any] = [work_id, parent_id] if parent_id else [work_id]
                cur = await self._db.execute(
                    f"SELECT id FROM nodes WHERE work_id = ? AND {link} ORDER BY path", args)
                ids = [r["id"] for r in await cur.fetchall()]
                if node_id in ids and before_id in ids:
                    if ids.index(before_id) == ids.index(node_id) + 1:
                        return {"moved": False, "reason": "已经在这个位置"}

            now = _now()
            if before:
                # 先落到目标父节点下的临时 path，再把这一层兄弟按 path 重排。
                # 两段式（全部先挪到 ~i，再写 0001..N）是为了避开
                # UNIQUE(work_id, path)：插到最前面时，0001 还占着位置。
                prefix = f"{target['path']}." if target else ""
                temp = f"{prefix}~{node_id.replace('_', '')}"
                new_depth = int(target["depth"]) + 1 if target else 0
                await self._repath_locked(work_id, me["path"], temp)
                await self._db.execute(
                    "UPDATE nodes SET depth = depth + ? "
                    "WHERE work_id = ? AND (path = ? OR path LIKE ?)",
                    (new_depth - int(me["depth"]), work_id, temp, f"{temp}.%"),
                )
                await self._db.execute(
                    "UPDATE nodes SET parent_id = ?, updated_at = ? WHERE id = ?",
                    (parent_id, now, node_id),
                )
                link = "parent_id = ?" if parent_id else "parent_id IS NULL"
                args = [work_id, parent_id] if parent_id else [work_id]
                cur = await self._db.execute(
                    f"SELECT id FROM nodes WHERE work_id = ? AND {link} ORDER BY path", args)
                ids = [r["id"] for r in await cur.fetchall()]
                ids.remove(node_id)
                ids.insert(ids.index(before_id), node_id)
                await self._reorder_siblings_locked(work_id, parent_id, prefix, ids)
            else:
                link = "parent_id = ?" if parent_id else "parent_id IS NULL"
                args = [work_id, parent_id] if parent_id else [work_id]
                cur = await self._db.execute(
                    f"SELECT path FROM nodes WHERE work_id = ? AND {link} ORDER BY path DESC LIMIT 1",
                    args,
                )
                last = await cur.fetchone()
                same_parent = (me["parent_id"] or None) == (parent_id or None)
                at_end = bool(same_parent and last and last["path"] == me["path"])

                cur = await self._db.execute(
                    "SELECT ordinal FROM nodes WHERE work_id = ? AND ordinal IS NOT NULL ORDER BY path",
                    (work_id,),
                )
                ords = [int(r["ordinal"]) for r in await cur.fetchall()]
                if at_end and ords == list(range(1, len(ords) + 1)):
                    return {"moved": False, "reason": "已经在这一卷末尾"}

                if not at_end:
                    seq = await self._next_seq(work_id, parent_id)
                    new_path = f"{target['path'] + '.' if target else ''}{seq:04d}"
                    new_depth = int(target["depth"]) + 1 if target else 0
                    # 先把 path 换掉，depth 按差值整棵子树平移（叶子没有子树，
                    # 但接口不该因为「现在没有」就在将来悄悄错位）
                    await self._repath_locked(work_id, me["path"], new_path)
                    await self._db.execute(
                        "UPDATE nodes SET depth = depth + ? "
                        "WHERE work_id = ? AND (path = ? OR path LIKE ?)",
                        (new_depth - int(me["depth"]), work_id, new_path, f"{new_path}.%"),
                    )
                    await self._db.execute(
                        "UPDATE nodes SET parent_id = ?, updated_at = ? WHERE id = ?",
                        (parent_id, now, node_id),
                    )

            renumbered = await self._renumber_locked(work_id)
            await self._db.execute(
                "UPDATE works SET updated_at = ? WHERE id = ?", (now, work_id)
            )
            await self._mark_index_dirty_locked(work_id)
            await self._db.commit()

            cur = await self._db.execute("SELECT ordinal FROM nodes WHERE id = ?", (node_id,))
            ordinal = int((await cur.fetchone())["ordinal"])
        result = {
            "moved": True,
            "ordinal": ordinal,
            "parent_title": target["title"] if target else "",
            "renumbered": renumbered,
        }
        if before:
            result["before_title"] = before["title"]
        return result

    async def _reorder_siblings_locked(
        self, work_id: str, parent_id: Optional[str], prefix: str,
        ordered_ids: list[str],
    ) -> None:
        """按给定顺序重排一层兄弟的 path。**调用方必须已持有 self._lock**。

        两段式（先整体挪到 `~i`，再写 `0001..N`）是为了避开
        `UNIQUE(work_id, path)`：插到最前面时，`0001` 还占着位置。
        reparent 的「插到某节之前」与 duplicate 的「复制到原节之后」
        都用这一套：三处各写一遍的话，迟早只有一处修 UNIQUE 的坑。
        """
        assert self._db is not None
        for i, cid in enumerate(ordered_ids):
            cur = await self._db.execute("SELECT path FROM nodes WHERE id = ?", (cid,))
            row = await cur.fetchone()
            if not row:
                raise ValueError(f"兄弟节点不存在: {cid}")
            await self._repath_locked(work_id, row["path"], f"{prefix}~{i}")
        for i, cid in enumerate(ordered_ids):
            await self._repath_locked(work_id, f"{prefix}~{i}", f"{prefix}{i + 1:04d}")

    @staticmethod
    def _subtree_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把子树按「父先于子、同层按 path」排——克隆插回时必须按这个顺序建。

        `parent_id` 有外键指向 `nodes(id)`，子行必须等父行插完才能写；
        depth 升序 + path 恰好就是这个顺序（path 字典序本来就是「先序即树序」）。
        """
        return sorted(rows, key=lambda r: (int(r["depth"]), str(r["path"])))

    async def duplicate_node(self, node_id: str) -> Optional[dict[str, Any]]:
        """复制这一节连同整棵子树，插到原节的紧后方。

        只搬树上的壳：`content` / `summary`（节拍卡）/ `plan_json` / `status`
        跟着走；版本历史、摘要表、索引分块都不复制——新节的内容
        是自己正文的初稿，不是原节的历史。字数不进写作日历：那是复制不是创作。

        插完按树序重排全书章号（`_renumber_locked`）并标脏索引台账，
        与 reparent 同一套收口。返回 `{ok, new_node_id, copied, renumbered}`；节点不存在返回 None。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,))
            me = await cur.fetchone()
            if not me:
                return None
            me = dict(me)
            work_id = me["work_id"]

            # 整棵子树（含自己）：删除用的那套谓词（等于 id 或 path 前缀）在这里同样成立
            cur = await self._db.execute(
                "SELECT * FROM nodes WHERE work_id = ? AND (id = ? OR path LIKE ?)",
                (work_id, node_id, f"{me['path']}.%"))
            rows = [dict(r) for r in await cur.fetchall()]
            cur = await self._db.execute("SELECT kind FROM works WHERE id = ?", (work_id,))
            work_row = await cur.fetchone()
            if not work_row:
                return None

            new_ids: dict[str, str] = {r["id"]: _new_id("n_") for r in rows
                                       if str(r["path"]) == me["path"]
                                       or str(r["path"]).startswith(f"{me['path']}.")}
            by_id = {r["id"]: r for r in rows}
            clone_root = new_ids[node_id]
            parent_id = me["parent_id"]
            prefix = f"{me['path'].rsplit('.', 1)[0]}." if "." in me["path"] else ""

            # 克隆整棵子树先落在同层临时 path（带子树后缀，不撞兄弟的 0001..N）：
            # 库里有了这些行，后面的兄弟重排才能把它们当真实节点排序
            # （与 reparent 的 before 分支同一段两段式临时名逻辑）
            now = _now()
            try:
                await self._db.execute(
                    "INSERT INTO nodes(id, work_id, parent_id, path, depth, ordinal, kind, title, "
                    "content, status, word_count, content_hash, summary, plan_json, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (clone_root, work_id, parent_id, f"{prefix}~{clone_root.replace('_', '')}",
                     int(me["depth"]), me["ordinal"], me["kind"], me["title"], me["content"], me["status"],
                     int(me["word_count"] or 0), me["content_hash"] or "",
                     me["summary"] or "", me["plan_json"] or "{}", now, now))
                child_rows = self._subtree_order([r for r in rows if r["id"] != node_id])
                for r in child_rows:
                    # 父在子前：新父要么本轮已插（在 new_ids 里查到），要么就是原父（根克隆的 parent）
                    np = new_ids.get(r["parent_id"]) or parent_id
                    cur = await self._db.execute("SELECT path FROM nodes WHERE id = ?", (np,))
                    prow = await cur.fetchone()
                    if not prow:
                        raise ValueError(f"新父节点不在库里: {np}")
                    pp = prow["path"]
                    rel = str(r["path"]).removeprefix(str(by_id[r["parent_id"]]["path"])).lstrip(".")
                    await self._db.execute(
                        "INSERT INTO nodes(id, work_id, parent_id, path, depth, ordinal, kind, title, "
                        "content, status, word_count, content_hash, summary, plan_json, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_ids[r["id"]], work_id, np, f"{pp}.{rel}", int(r["depth"]),
                         r["ordinal"], r["kind"], r["title"], r["content"], r["status"],
                         int(r["word_count"] or 0), r["content_hash"] or "",
                         r["summary"] or "", r["plan_json"] or "{}", now, now))

                # 把临时名里的克隆根（只根这一层）插到原节之后，整层重排 path；
                # 克隆子树跟着根走——_repath_locked 换的是整段前缀
                link = "parent_id = ?" if parent_id else "parent_id IS NULL"
                args: list[Any] = [work_id] + ([parent_id] if parent_id else [])
                cur = await self._db.execute(
                    f"SELECT id FROM nodes WHERE work_id = ? AND {link} ORDER BY path", args)
                sib_ids = [r["id"] for r in await cur.fetchall()]
                sib_ids.remove(clone_root)
                sib_ids.insert(sib_ids.index(node_id) + 1, clone_root)
                await self._reorder_siblings_locked(work_id, parent_id, prefix, sib_ids)

                renumbered = await self._renumber_locked(work_id)
                await self._db.execute(
                    "UPDATE works SET updated_at = ? WHERE id = ?", (now, work_id))
                await self._mark_index_dirty_locked(work_id)
                await self._db.commit()
            except BaseException:
                # 事务半途失败必须回滚：不然这半截克隆留在连接里，
                # 会被下一次写操作的 commit 一起带进库（merge_nodes 同一口径）
                await self._db.rollback()
                raise
        return {"ok": True, "new_node_id": clone_root, "copied": len(rows),
                "renumbered": renumbered}

    async def split_node(self, node_id: str, offsets: list[int]) -> Optional[dict[str, Any]]:
        """按字符偏移把一节的正文切成若干节，新节紧跟在原节后面。

        `offsets` 是切割点（正文下标，升序、不含首尾），第 i 段留在原节，
        其余各段成为同级兄弟节——与 reparent 同层，不跨容器。
        有子节点的容器切不了：正文在章上而子节跟着章走，切开之后那套
        层级没有合理解释，让人自己在树上搬。

        原节的节拍卡（summary）不自动分给两段——节拍卡写的是整节的意思，
        猜一半是编数据；置空后由计划页重抽。plan_json 各段都带上同一份计划。
        """
        assert self._db is not None
        clean = sorted({int(o) for o in offsets or [] if 0 < int(o)})
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
            row = await cur.fetchone()
            if not row:
                return None
            me = dict(row)
            work_id = me["work_id"]
            content = me["content"] or ""
            if not clean:
                return {"ok": True, "node_id": node_id, "parts": [node_id], "split": False}
            if max(clean) >= len(content):
                raise ValueError(f"切割点越界：正文只有 {len(content)} 字")
            cur = await self._db.execute(
                "SELECT 1 FROM nodes WHERE parent_id = ? LIMIT 1", (node_id,))
            if await cur.fetchone():
                raise ValueError("这一节下面还有子节点：切分只针对有正文的叶子")

            now = _now()
            try:
                bounds = [0, *clean, len(content)]
                parts = [content[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
                # 原节只留第一段：正文变了，下游摘要与索引按 update 同一套规则失效
                await self._db.execute(
                    "UPDATE nodes SET content = ?, word_count = ?, content_hash = ?, summary = '', "
                    "updated_at = ? WHERE id = ?",
                    (parts[0], _count_words(parts[0]), _hash(parts[0]) if parts[0] else "",
                     now, node_id))
                await self._mark_stale_locked(work_id, node_id)

                new_ids: list[str] = []
                for i, part in enumerate(parts[1:], start=2):
                    nid = _new_id("n_")
                    title = self._part_title(me["title"], i)
                    seq = await self._next_seq(work_id, me["parent_id"])
                    ppath = ""
                    if me["parent_id"]:
                        cur = await self._db.execute(
                            "SELECT path FROM nodes WHERE id = ?", (me["parent_id"],))
                        ppath = (await cur.fetchone())["path"] + "."
                    await self._db.execute(
                        "INSERT INTO nodes(id, work_id, parent_id, path, depth, ordinal, kind, title, "
                        "content, status, word_count, content_hash, summary, plan_json, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (nid, work_id, me["parent_id"], f"{ppath}{seq:04d}", int(me["depth"]),
                         me["ordinal"], me["kind"], title, part, "draft",
                         _count_words(part), _hash(part) if part else "", "",
                         me["plan_json"] or "{}", now, now))
                    new_ids.append(nid)
                    await self._mark_stale_locked(work_id, nid)
                # 新节按插入顺序落在原节后面：重排这一层，把刚追到末尾的几节插回原位
                link = "parent_id = ?" if me["parent_id"] else "parent_id IS NULL"
                args = [work_id, me["parent_id"]] if me["parent_id"] else [work_id]
                cur = await self._db.execute(
                    f"SELECT id FROM nodes WHERE work_id = ? AND {link} ORDER BY path", args)
                ids = [r["id"] for r in await cur.fetchall()]
                # 倒着插才正：每次都插到「紧挨原节」的位置，后插的把先插的往后顶
                for nid in reversed(new_ids):
                    ids.remove(nid)
                    ids.insert(ids.index(node_id) + 1, nid)
                prefix = ppath
                await self._reorder_siblings_locked(work_id, me["parent_id"], prefix, ids)
                renumbered = await self._renumber_locked(work_id)
                await self._db.execute(
                    "UPDATE works SET updated_at = ? WHERE id = ?", (now, work_id))
                await self._mark_index_dirty_locked(work_id)
                await self._db.commit()
            except BaseException:
                # 切到一半失败（越界、重排撞键）不能把半截段落留在连接里：
                # 不回滚的话，下一次写操作的 commit 会把这些没成形的节一并带进库
                await self._db.rollback()
                raise
        return {"ok": True, "node_id": node_id, "parts": [node_id, *new_ids],
                "split": True, "renumbered": renumbered}

    @staticmethod
    def _part_title(base: str, index: int) -> str:
        """拆分出的第 index 段的名字：结尾已经是数字的直接追加，否则加括号后缀。"""
        base = (base or "").strip() or "未命名"
        if base and base[-1].isdigit():
            return f"{base}{index}"[:60]
        return f"{base}（{index}）"[:60]

    async def merge_nodes(
        self, node_id: str, count: int = 2, separator: str = "\n\n",
    ) -> Optional[dict[str, Any]]:
        """把连续几节合并成一节：正文按顺序拼进第一节，其余节连同子树删除。

        只吃「没有子节点的叶子节」：章带节时「合并两章」真正要合的是两棵子树，
        那一版先不做，把树搬来搬去交给拖拽。被吸收的节在删除前会像
        `delete_node` 一样先落备份——合并本质上是一次删除，退路必须一样。

        第一节的节拍卡不自动拼接（与拆分同一取向），置空重抽；
        删完同样走章号重排与索引标脏。
        """
        assert self._db is not None
        n = max(2, int(count))
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
            row = await cur.fetchone()
            if not row:
                return None
            me = dict(row)
            work_id = me["work_id"]
            if me["ordinal"] is None:
                raise ValueError("容器不参与合并：只合有正文的章与节")
            link = "parent_id = ?" if me["parent_id"] else "parent_id IS NULL"
            args: list[Any] = [work_id] + ([me["parent_id"]] if me["parent_id"] else [])
            cur = await self._db.execute(
                f"SELECT id FROM nodes WHERE work_id = ? AND {link} ORDER BY path", args)
            ids = [r["id"] for r in await cur.fetchall()]
            idx = ids.index(node_id)
            absorb_ids = ids[idx + 1: idx + n]
            if not absorb_ids:
                raise ValueError("后面没有可合并的节了")

            targets: list[dict[str, Any]] = []
            for aid in absorb_ids:
                cur = await self._db.execute("SELECT * FROM nodes WHERE id = ?", (aid,))
                t = await cur.fetchone()
                if not t:
                    raise ValueError(f"节点不存在: {aid}")
                t = dict(t)
                if t["ordinal"] is None:
                    raise ValueError("遇到容器节点：只合有正文的章与节")
                cur = await self._db.execute(
                    "SELECT 1 FROM nodes WHERE parent_id = ? LIMIT 1", (aid,))
                if await cur.fetchone():
                    raise ValueError("这一节下面还有子节点：合并只吃叶子")
                targets.append(t)

            sep = separator if separator.strip() else "\n\n"
            pieces = [me["content"] or ""] + [t["content"] or "" for t in targets]
            merged = sep.join(p for p in pieces if p)
            titles = [me["title"]] + [t["title"] for t in targets]
            title = me["title"]
            if len("／".join(titles)) <= 60:
                title = "／".join(titles)
            now = _now()
            try:
                await self._db.execute(
                    "UPDATE nodes SET content = ?, word_count = ?, content_hash = ?, title = ?, "
                    "summary = '', updated_at = ? WHERE id = ?",
                    (merged, _count_words(merged), _hash(merged) if merged else "",
                     title, now, node_id))
                await self._mark_stale_locked(work_id, node_id)

                for t in targets:
                    # 与 delete_node 同一条路：每删一节先落备份。整段在同一次持锁里、
                    # 最后才 commit：备份写不成（BackupError）时下面的回滚把正文拼接一起抹掉，
                    # 不会留下「正文已拼好、原节没备份」的半截状态
                    bundle = await self._node_backup_bundle_locked(t["id"])
                    if bundle is None:
                        raise ValueError(f"节点不存在: {t['id']}")
                    self._write_node_backup(bundle)
                    where = "work_id = ? AND (id = ? OR path LIKE ?)"
                    params = (work_id, t["id"], f"{bundle['path']}.%")
                    cur = await self._db.execute(f"SELECT id FROM nodes WHERE {where}", params)
                    await self._drop_unit_rows("node", [r["id"] for r in await cur.fetchall()])
                    await self._db.execute(f"DELETE FROM nodes WHERE {where}", params)

                renumbered = await self._renumber_locked(work_id)
                await self._db.execute(
                    "UPDATE works SET updated_at = ? WHERE id = ?", (now, work_id))
                await self._mark_index_dirty_locked(work_id)
                await self._db.commit()
            except BaseException:
                # 备份写不成或删到一半出错：这一阵子里的所有改动一起退回，
                # 不回滚的话它们会挂连接上、被下一次写操作顺手 commit 进库
                await self._db.rollback()
                raise
        return {"ok": True, "node_id": node_id, "merged": 1 + len(targets),
                "title": title, "word_count": _count_words(merged),
                "renumbered": renumbered}

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

    async def delete_node(self, node_id: str, *, backup: bool = True) -> bool:
        """删节点连同其整棵子树（靠 path 前缀，不依赖递归）。

        删之前先把这一子树连同全部历史版本写一份 JSON。备份写不成就直接拒绝删除：
        宁可删不掉（重试、或者去把目录权限改对），不能删了找不回。
        备份落在 `storage/backups/nodes/`，目录由库路径推出来，不另开配置项。

        快照、落盘、删除必须放在**同一次持锁**里做完。锁一放，等锁的后台队列或另一个
        标签页的 `create_node` 就能插进这棵子树里，接着被 `path LIKE` 前缀连带删掉——
        备份里没有它，等于删了还宣称备份过。单机低频，持锁写一个本地小文件不心疼。
        """
        assert self._db is not None
        async with self._lock:
            bundle = await self._node_backup_bundle_locked(node_id)
            if bundle is None:
                return False
            if backup:
                self._write_node_backup(bundle)
            # 删除谓词直接用刚才那份快照里的 work_id / path：同一次锁内它们不会变，
            # 再查一遍反而多给一次「查到的子树与备份里的不是一套」的机会
            where = "work_id = ? AND (id = ? OR path LIKE ?)"
            params = (bundle["work_id"], node_id, f"{bundle['path']}.%")
            # 子树 id 要和上面删节点的谓词完全一致，不然清掉的行和删掉的节点对不上
            cur = await self._db.execute(f"SELECT id FROM nodes WHERE {where}", params)
            await self._drop_unit_rows("node", [r["id"] for r in await cur.fetchall()])
            await self._db.execute(f"DELETE FROM nodes WHERE {where}", params)
            await self._db.commit()
        return True

    # ==================== 删除备份与找回 ====================

    def _backup_dir(self, kind: str, action: str = "删除") -> Path:
        """备份目录：`storage/studio.sqlite` → `storage/backups/<kind>/`。不存在就建。

        `action` 只影响报错措辞：快照失败不能说「已拒绝删除」，那是在讲另一件事。
        """
        target = self.db_path.parent / "backups" / kind
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 包一层：调用方只需要知道「备份写不成」
            # 完整原因进日志：detail 会显示到界面上，不能把库在本机的位置说出去
            logger.error(f"[studio] 备份目录不可用 {target}: {exc}")
            raise BackupError(f"备份目录不可用，已拒绝{action}") from exc
        return target

    def _stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    async def _write_db_snapshot(
        self, kind: str, prefix: str, keep: int, action: str,
    ) -> Path:
        """用 `VACUUM INTO` 往 `backups/<kind>/` 取一份一致快照。

        不学 `scripts/cleanup_works.py` 用 `shutil.copy2`：那条只是维护脚本顺手做，
        这一份却是稿子的唯一退路。本库是默认的 rollback journal 模式，热拷贝既不参与
        SQLite 的文件锁协议、也不会把 `-journal` 一起拷走，撞上在途写事务时拷出来那份
        要么打不开、要么少了最近几笔——比没有备份更坏，它把人的心理保险买走了。
        """
        assert self._db is not None
        dest: Optional[Path] = None
        try:
            folder = self._backup_dir(kind, action=action)
            stamp = self._stamp()
            dest = folder / f"{prefix}-{stamp}.sqlite"
            # `VACUUM INTO` 遇同名文件会报错，而时间戳只到秒：同一秒连着取两本就撞上了
            n = 1
            while dest.exists():
                dest = folder / f"{prefix}-{stamp}_{n}.sqlite"
                n += 1
            # 文件名也要在 try 里拼：时间戳取不到同样是「备份没写成」，不能漏成裸 OSError 冒出去
            await self._db.commit()   # VACUUM 不能在事务里跑：先把在途改动落盘
            await self._db.execute("VACUUM INTO ?", (str(dest),))
        except BackupError:
            raise                          # _backup_dir 已经把话说清楚了，不再套一层
        except Exception as exc:           # 不止 OSError：SQL 报错、磁盘满都算「备份没写成」
            logger.error(f"[studio] 整库副本没写成 {dest}: {exc}")
            raise BackupError(f"整库副本没写成，{action}已中止") from exc
        self._prune_quietly(kind, keep)
        return dest

    async def _copy_db_backup(self, label: str) -> Path:
        """删作品前的整库副本（`backups/works/`，留 `BACKUP_WORK_KEEP` 份）。"""
        return await self._write_db_snapshot(
            "works", f"studio-before-delete-{label}", BACKUP_WORK_KEEP, "删除")

    # ---- 每日快照：库文件坏了也有一条退路 ----

    async def _last_change_ts(self) -> float:
        """库里最后一次改动的时间戳（`nodes` 与 `works` 的 `updated_at` 取大者）。

        用它判「今天有没有写过字」而不是文件的 mtime：整本 VACUUM、换机拷文件都会动
        mtime，而正文一个字没变。改标题、改状态、建节、存正文都会写这一列。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT MAX(x) FROM ("
                "SELECT MAX(updated_at) AS x FROM nodes "
                "UNION ALL SELECT MAX(updated_at) FROM works)")
            row = await cur.fetchone()
        return float(row[0] or 0.0) if row else 0.0

    async def _has_any_work(self) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT COUNT(*) FROM works")
            row = await cur.fetchone()
        return bool(row) and int(row[0]) > 0

    async def _take_db_snapshot(self, prefix: str) -> Path:
        """取一份每日/手动快照，并在锁内做。

        锁不是礼节：`VACUUM` 要自己开一个事务，另一个协程正卡在写事务中间时它只能失败；
        `delete_work` 那条路径本来就持着锁走到 `_copy_db_backup`，这里补齐同样的前提。
        库实测 1 MB 出头，锁只持有几十毫秒，不会把前台写入卡住。
        """
        async with self._lock:
            return await self._write_db_snapshot(
                "snapshots", prefix, BACKUP_SNAPSHOT_KEEP, "快照")

    async def maybe_daily_snapshot(self) -> Optional[Path]:
        """每天第一次真改过正文后取一份整库快照，留 `BACKUP_SNAPSHOT_KEEP` 份。

        三个条件全过才动手，缺一个就什么都不做（返回 None）：空库不占名额、
        没写过字不产重复副本、今天已经取过不再取第二次。

        写不成也只返回 None：这一条是后台记账，它自己不拥有任何拒绝的权力。
        账不写回去，下一轮（半小时后）会再看一眼，不会因为这一下失败就永远不再取。

        为什么需要它：`backups/works/` 那一份只在**删作品**那一刻写，而真实事故是
        「写了三个月，库文件坏了 / 磁盘没了」——那种情况下没有任何一份副本。
        """
        assert self._db is not None
        change = await self._last_change_ts()
        if not change:
            return None
        if not await self._has_any_work():
            return None      # 还没建过作品：空库不值得留一份副本
        today = datetime.now().date().isoformat()
        if await self.get_setting(SNAPSHOT_DAY_KEY) == today:
            return None
        prev = _to_float(await self.get_setting(SNAPSHOT_TS_KEY))
        if change <= prev:
            # 上次快照之后一个字没改：今天这一份和昨天那份内容相同，白占一个名额
            return None
        try:
            dest = await self._take_db_snapshot("studio-daily")
        except (BackupError, OSError) as exc:
            logger.warning(f"[studio] 每日库快照没写成，下一轮再看：{exc}")
            return None
        await self.set_setting(SNAPSHOT_DAY_KEY, today)
        await self.set_setting(SNAPSHOT_TS_KEY, str(change))
        logger.info(f"[studio] 每日库快照已写：{dest.name}")
        return dest

    async def snapshot_now(self) -> Path:
        """立刻取一份整库快照（控制台那个按钮）：跳过「今天已有 / 没改动」两道判断。

        人按下去就是要一份，不跟他讲条件。仍然把 `snapshot_last_change` 记上，
        免得后台循环紧接着又补一份内容相同的；但**不记 `snapshot_last_day`**：
        早上手动一份不该把今天的自动那份顶掉，否则白天写了一天、晚上崩了就没退路。
        """
        dest = await self._take_db_snapshot("studio-manual")
        await self.set_setting(SNAPSHOT_TS_KEY, str(await self._last_change_ts()))
        logger.info(f"[studio] 手动库快照已写：{dest.name}")
        return dest

    async def _node_backup_bundle_locked(self, node_id: str) -> Optional[dict[str, Any]]:
        """读一份删除前的完整快照：这一节 + 整棵子树 + 每个节点的全部历史版本。

        **调用方必须已持有 self._lock**，而且要与真正的删除在同一次持锁里，理由见 `delete_node`。
        历史版本逐节点全量取、不限量不采样：`node_versions` 挂在 `ON DELETE CASCADE` 上，
        这一份 JSON 是它们在这个世界上剩下的唯一一份。
        """
        assert self._db is not None
        cur = await self._db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,))
        root = await cur.fetchone()
        if not root:
            return None
        cur = await self._db.execute(
            "SELECT * FROM nodes WHERE work_id = ? AND (id = ? OR path LIKE ?) ORDER BY path",
            (root["work_id"], node_id, f"{root['path']}.%"))
        rows = await cur.fetchall()
        nodes: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            vc = await self._db.execute(
                "SELECT version_no, content, source, run_id, created_at "
                "FROM node_versions WHERE node_id = ? ORDER BY version_no", (d["id"],))
            d["versions"] = [dict(v) for v in await vc.fetchall()]
            nodes.append(d)
        return {
            "work_id": root["work_id"], "node_id": node_id, "title": root["title"],
            "kind": root["kind"], "path": root["path"], "parent_id": root["parent_id"],
            "ordinal": root["ordinal"], "status": root["status"],
            "deleted_at": _now(), "nodes": nodes,
        }

    def _write_node_backup(self, bundle: dict[str, Any]) -> Path:
        name = (f"{bundle['work_id']}__{bundle['node_id']}__"
                f"{datetime.fromtimestamp(bundle['deleted_at']).strftime('%Y%m%d_%H%M%S')}.json")
        target = self._backup_dir("nodes") / name
        # 先写临时名再换：直接写最终文件名时，盘满或进程被杀会留下半截 JSON，
        # 而它 mtime 最新，会被保留策略当成「最近一份」留在盘上，到恢复时才发现是坏的
        part = Path(f"{target}.part")
        try:
            part.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(part, target)
        except Exception as exc:   # 不止 OSError：正文里有异常内容时 json.dumps 会抛 TypeError
            part.unlink(missing_ok=True)
            logger.error(f"[studio] 节点备份没写成 {target}: {exc}")
            raise BackupError("备份没写成，已拒绝删除") from exc
        self._prune_quietly("nodes", BACKUP_NODE_KEEP)
        return target

    def backup_dir(self, kind: str) -> Path:
        """备份目录的对外入口（接口与脚本用它定位文件，不在外面拼路径）。"""
        return self._backup_dir(kind)

    def list_db_backups(self, kind: str) -> list[dict[str, Any]]:
        """整库类备份列举（新在前）：名字 / 大小 / 时间。找回靠 `scripts/restore_backup.py`。"""
        rows: list[dict[str, Any]] = []
        for p in self._backup_dir(kind).glob(_BACKUP_GLOB[kind]):
            st = _backup_stat(p)
            if st is None:
                continue     # 正好被并发清理摸掉了：少一行列表，不该让整个请求报错
            rows.append({"name": p.name, "bytes": st[1], "created_at": st[0]})
        rows.sort(key=lambda r: (r["created_at"], r["name"]), reverse=True)
        return rows

    def list_work_backups(self) -> list[dict[str, Any]]:
        return self.list_db_backups("works")

    def list_snapshot_backups(self) -> list[dict[str, Any]]:
        return self.list_db_backups("snapshots")

    def prune_backups(self, kind: str, keep: int) -> list[str]:
        """只留最近 `keep` 份，返回被清掉的文件名。

        按修改时间排而不是按文件名：节点备份名以 work id 开头，字典序不等于时间序。
        只扫 `_BACKUP_GLOB` 里那一种后缀，与列举口径保持一致：临时名留下的 `.part` 是
        没写成或进程被杀剩的半截文件，mtime 最新，算进「留最近 N 份」就会把一份真备份
        挤出去；而别人顺手丢进这个目录的东西更不该被清理当成旧备份删掉。
        当前库本体在上一层目录，摸不到。"""
        timed: list[tuple[float, Path]] = []
        for p in self._backup_dir(kind).glob(_BACKUP_GLOB.get(kind, "*")):
            st = _backup_stat(p)
            if st is not None:
                timed.append((st[0], p))
        timed.sort(key=lambda x: (x[0], x[1].name))
        gone: list[str] = []
        for _, stale in timed[: max(0, len(timed) - max(1, keep))]:
            try:
                stale.unlink()
                gone.append(stale.name)
            except OSError:
                logger.warning(f"[studio] 旧备份删不掉，先留着：{stale}")
        return gone

    def _prune_quietly(self, kind: str, keep: int) -> None:
        """删除路径上的清理：这是记账，不该有拦住删除的权力。

        备份明明已经写成，却因为它扫目录或某个文件摸不到而抛错，用户看到的是「删不掉」，
        而那恰好是我们最不想要的结果。失败就留在日志里，旧备份多躺一轮不伤任何人。
        """
        try:
            self.prune_backups(kind, keep)
        except Exception as exc:
            logger.warning(f"[studio] 旧备份清理失败，先留着不管：{exc}")

    def list_node_backups(self) -> list[dict[str, Any]]:
        """只读列举节点备份（新在前），不抛异常也不改任何东西。"""
        rows: list[dict[str, Any]] = []
        for f in self._backup_dir("nodes").glob(_BACKUP_GLOB["nodes"]):
            st = _backup_stat(f)
            if st is None:
                continue
            row: dict[str, Any] = {
                "name": f.name, "bytes": st[1], "created_at": st[0],
                "title": "", "work_id": "", "kind": "", "node_count": 0,
            }
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("备份顶层不是对象")
                row.update(title=data.get("title") or "", work_id=data.get("work_id") or "",
                           kind=data.get("kind") or "",
                           node_count=len(data.get("nodes") or []))
            except (OSError, ValueError):
                row["title"] = "（读不了，文件可能坏了）"
            rows.append(row)
        rows.sort(key=lambda r: (r["created_at"], r["name"]), reverse=True)
        return rows

    async def restore_node_backup(self, name: str) -> Optional[dict[str, Any]]:
        """从备份 JSON 找回一节（连子树与历史版本一起）。

        口径：原父节点还在就挂回它下面，没了就挂到作品根（宁可位置不对，不能造孤儿）；
        节点一律用新 id，旧 id 已被级联删掉、而且同一个备份可能被恢复两次；
        path 重新排、ordinal 追加到末尾——原序就记在 bundle 里，恢复后用上下移挪回去。
        返回 None = 备份不存在、读不懂、或那本书已经整本删了（那就只能走整库副本）。

        恢复等于往库里加回一整棵子树的正文，所以末尾要把上游的卷/全书摘要判过期、
        并把库标脏：不这么做，摘要还停留在「没有这一节」的那一版上却自称新鲜，
        assembler 照着注入，右栏的过期计数也看不见——那正是「改完稿摘要还在撒谎」那一类。
        备份只带走 nodes 与 node_versions：摘要、告警、生成记录不在里面，
        所以恢复后那一节的摘要要重新生成一次，历史版本则是全数带回来的。
        """
        path = self._backup_dir("nodes") / name
        if not path.is_file():
            return None
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"[studio] 备份读不了 {name}: {exc}")
            return None
        # 判据与脚本共用同一个 `check_node_bundle`：两处各写一套，脚本报的原因就会说谎
        reason = check_node_bundle(bundle)
        if reason:
            logger.warning(f"[studio] 备份结构不对（{reason}）：{name}")
            return None
        rows = bundle["nodes"]
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT 1 FROM works WHERE id = ?", (bundle["work_id"],))
            if not await cur.fetchone():
                return None
            now = _now()
            restored: dict[str, str] = {}
            made: list[dict[str, Any]] = []
            # bundle 里的 nodes 是按 path 排的，父在子前，所以能一层层挂上去
            for src in rows:
                old_parent = src.get("parent_id")
                parent_id: Optional[str] = None
                parent_path, depth = "", 0
                if old_parent:
                    if old_parent in restored:  # 本次恢复的子树内部，用新 id
                        parent_id = restored[old_parent]
                        cur = await self._db.execute(
                            "SELECT path, depth FROM nodes WHERE id = ?", (parent_id,))
                        prow = await cur.fetchone()
                        parent_path, depth = prow["path"] + ".", int(prow["depth"]) + 1
                    else:
                        cur = await self._db.execute(
                            "SELECT path, depth FROM nodes WHERE id = ?", (old_parent,))
                        prow = await cur.fetchone()
                        if prow:
                            parent_id = old_parent
                            parent_path, depth = prow["path"] + ".", int(prow["depth"]) + 1
                seq = await self._next_seq(bundle["work_id"], parent_id)
                ordinal: Optional[int] = None
                if src.get("ordinal") is not None:
                    cur = await self._db.execute(
                        "SELECT COALESCE(MAX(ordinal),0) AS m FROM nodes WHERE work_id = ?",
                        (bundle["work_id"],))
                    ordinal = int((await cur.fetchone())["m"]) + 1
                nid = _new_id("n_")
                content = src.get("content") or ""
                await self._db.execute(
                    "INSERT INTO nodes(id, work_id, parent_id, path, depth, ordinal, kind, title, "
                    "content, status, word_count, content_hash, summary, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, bundle["work_id"], parent_id, f"{parent_path}{seq:04d}", depth,
                     ordinal, src["kind"], src["title"], content, src.get("status") or "draft",
                     _count_words(content), _hash(content) if content else "",
                     src.get("summary") or "", now, now),
                )
                for v in src.get("versions") or []:
                    await self._db.execute(
                        "INSERT INTO node_versions(id, node_id, version_no, content, source, "
                        "run_id, created_at) VALUES(?,?,?,?,?,?,?)",
                        (_new_id("v_"), nid, int(v["version_no"]), v["content"],
                         v.get("source") or "human", v.get("run_id"), v.get("created_at") or now),
                    )
                restored[src["id"]] = nid
                made.append({"id": nid, "title": src["title"], "kind": src["kind"],
                             "ordinal": ordinal, "versions": len(src.get("versions") or []),
                             # 原父节点已不在，这一节被挂到了作品根：返回体里得说出来
                             "hung_on_root": bool(parent_id is None and old_parent)})
            await self._db.execute(
                "UPDATE works SET updated_at = ? WHERE id = ?", (now, bundle["work_id"]))
            # 找回来的这一棵子树是多出来的正文：上游摘要必须重新判过期（口径见方法 docstring）
            new_root = restored.get(bundle.get("node_id") or "")
            if new_root:
                await self._mark_stale_locked(bundle["work_id"], new_root)
            await self._mark_index_dirty_locked(bundle["work_id"])
            await self._db.commit()
        return {
            "restored": made, "work_id": bundle["work_id"],
            "root_id": restored.get(bundle.get("node_id") or "", ""),
            "original_ordinal": bundle.get("ordinal"),
        }

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
        # 判据抽到 `preserve_current_version`：批量替换走的是同一句，两处不会写出两套规则
        await self.preserve_current_version(node_id, current)
        # 回滚不是「写」：换回旧稿不该把当天的字数刷高
        await self.update_node(node_id, content=target["content"] or "",
                               snapshot_source="restore")
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

    async def update_source(
        self, source_id: str, *, title: Optional[str] = None, url: Optional[str] = None,
        published_at: Optional[str] = None, content: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """改一条素材的标题/出处/正文。

        这里只写字段，不碰 chunks：要不要重建索引交给 `indexer.index_source`
        按 content_hash 判断——只改标题时它会自己跳过，不用在这里分叉。
        """
        updates = {
            "title": title, "url": url, "published_at": published_at, "content": content,
        }
        updates = {k: v for k, v in updates.items() if v is not None}
        if not updates:
            return await self.get_source(source_id)
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self._lock:
            cur = await self._db.execute("SELECT id FROM sources WHERE id = ?", (source_id,))
            if not await cur.fetchone():
                return None
            await self._db.execute(
                f"UPDATE sources SET {sets} WHERE id = ?", [*updates.values(), source_id]
            )
            await self._db.commit()
        return await self.get_source(source_id)

    async def mark_sources_used(self, source_ids: list[str]) -> int:
        """记一次「这条素材真的进了 prompt」。

        `sources.used` 从建表起就有，但一直没人写它，面板上也就没法告诉作者
        哪条素材是死重。只预览不算，必须真的发出去——所以预览接口不调这里。
        """
        ids = [i for i in dict.fromkeys(source_ids) if i]
        if not ids:
            return 0
        assert self._db is not None
        marks = ",".join("?" for _ in ids)
        async with self._lock:
            cur = await self._db.execute(
                f"UPDATE sources SET used = COALESCE(used, 0) + 1 WHERE id IN ({marks})",
                ids,
            )
            await self._db.commit()
        return cur.rowcount or 0

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

    async def update_fact(
        self, fact_id: str, *, statement: Optional[str] = None,
        confidence: Optional[float] = None, valid_until: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """改一张事实卡。出处（source_id）不在这里改：换来源等于换一条卡。"""
        updates = {
            "statement": statement, "confidence": confidence, "valid_until": valid_until,
        }
        updates = {k: v for k, v in updates.items() if v is not None}
        if not updates:
            return await self.get_fact(fact_id)
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self._lock:
            cur = await self._db.execute("SELECT id FROM facts WHERE id = ?", (fact_id,))
            if not await cur.fetchone():
                return None
            await self._db.execute(
                f"UPDATE facts SET {sets} WHERE id = ?", [*updates.values(), fact_id]
            )
            await self._db.commit()
        return await self.get_fact(fact_id)

    async def get_fact(self, fact_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT f.*, s.title AS source_title FROM facts f "
                "LEFT JOIN sources s ON s.id = f.source_id WHERE f.id = ?",
                (fact_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

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

    async def update_term(
        self, term_id: str, *, term: Optional[str] = None, definition: Optional[str] = None,
        preferred: Optional[str] = None, forbidden: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """改术语。改词本身会撞 `UNIQUE(work_id, term)`——那是「重名」，不是系统故障。"""
        import sqlite3

        updates: dict[str, Any] = {
            "term": term, "definition": definition, "preferred": preferred,
        }
        updates = {k: v for k, v in updates.items() if v is not None}
        if forbidden is not None:
            updates["forbidden"] = json.dumps(forbidden, ensure_ascii=False)
        if not updates:
            return await self.get_term(term_id)
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self._lock:
            cur = await self._db.execute("SELECT id FROM terms WHERE id = ?", (term_id,))
            if not await cur.fetchone():
                return None
            try:
                await self._db.execute(
                    f"UPDATE terms SET {sets} WHERE id = ?", [*updates.values(), term_id]
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("已经有同名术语了") from exc
            await self._db.commit()
        return await self.get_term(term_id)

    async def get_term(self, term_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM terms WHERE id = ?", (term_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    # ==================== 公众号：文风 / 设置 / 发布台账 ====================

    async def get_setting(self, key: str) -> str:
        """全局设置：文风画像、文末引导这类东西属于作者，不属于某一篇。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
        return str(row["value"] or "") if row else ""

    async def all_settings(self) -> dict[str, str]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT key, value FROM settings")
            rows = await cur.fetchall()
        return {str(r["key"]): str(r["value"] or "") for r in rows}

    async def set_setting(self, key: str, value: str) -> None:
        assert self._db is not None
        async with self._lock:
            await self._db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value or "", _now()),
            )
            await self._db.commit()

    async def add_voice_sample(self, *, content: str, title: str = "") -> dict[str, Any]:
        assert self._db is not None
        sid = _new_id("v_")
        async with self._lock:
            await self._db.execute(
                "INSERT INTO voice_samples(id, title, content, chars, created_at) "
                "VALUES(?,?,?,?,?)",
                (sid, title or "", content,
                 sum(1 for c in content if not c.isspace()), _now()),
            )
            await self._db.commit()
        return await self.get_voice_sample(sid)  # type: ignore[return-value]

    async def list_voice_samples(self) -> list[dict[str, Any]]:
        """列表不带正文：一篇旧文可能几千字，列表只要标题与字数。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, title, chars, created_at FROM voice_samples "
                "ORDER BY created_at DESC"
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_voice_sample(self, sample_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM voice_samples WHERE id = ?", (sample_id,)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_voice_texts(self) -> list[dict[str, Any]]:
        """统计要全文。单独一个方法，是为了别把几万字顺手塞进列表接口。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT title, content FROM voice_samples ORDER BY created_at ASC"
            )
            rows = await cur.fetchall()
        return [{"title": r["title"], "content": r["content"]} for r in rows]

    async def delete_voice_sample(self, sample_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "DELETE FROM voice_samples WHERE id = ?", (sample_id,)
            )
            await self._db.commit()
            return (cur.rowcount or 0) > 0

    async def add_publication(
        self, work_id: str, *, published_at: str = "", url: str = "",
        reads: int = 0, likes: int = 0, followers: int = 0, note: str = "",
    ) -> dict[str, Any]:
        assert self._db is not None
        pid = _new_id("p_")
        now = _now()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO publications(id, work_id, published_at, url, reads, likes, "
                "followers, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pid, work_id, published_at or "", url or "", max(0, int(reads)),
                 max(0, int(likes)), max(0, int(followers)), note or "", now, now),
            )
            await self._db.commit()
        return await self.get_publication(pid)  # type: ignore[return-value]

    async def get_publication(self, pub_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM publications WHERE id = ?", (pub_id,)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_publications(self, work_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM publications WHERE work_id = ? "
                "ORDER BY published_at DESC, created_at DESC",
                (work_id,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_publication(self, pub_id: str, **fields: Any) -> bool:
        allowed = {"published_at", "url", "reads", "likes", "followers", "note"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        assert self._db is not None
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self._lock:
            cur = await self._db.execute(
                f"UPDATE publications SET {sets} WHERE id = ?",
                (*updates.values(), pub_id),
            )
            await self._db.commit()
            return (cur.rowcount or 0) > 0

    async def delete_publication(self, pub_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "DELETE FROM publications WHERE id = ?", (pub_id,)
            )
            await self._db.commit()
            return (cur.rowcount or 0) > 0

    async def publication_summary(self) -> list[dict[str, Any]]:
        """书架用：每个作品发了几篇、总阅读、最后一次的链接。

        `last_url` 用相关子查询取「最新一条带链接的」：同一篇可能改过两次，
        空链接那条不该把往期回顾的位置占掉。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT p.work_id, w.title AS work_title, w.subtype AS subtype, "
                "COUNT(*) AS pubs, COALESCE(SUM(p.reads),0) AS reads, "
                "COALESCE(SUM(p.likes),0) AS likes, MAX(p.published_at) AS last_at, "
                "(SELECT p2.url FROM publications p2 WHERE p2.work_id = p.work_id "
                "  AND p2.url <> '' ORDER BY p2.published_at DESC, p2.created_at DESC "
                "  LIMIT 1) AS last_url "
                "FROM publications p JOIN works w ON w.id = p.work_id "
                "GROUP BY p.work_id ORDER BY last_at DESC"
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def archive_links(
        self, *, subtype: str = "", exclude_work_id: str = "", limit: int = 5,
    ) -> list[dict[str, Any]]:
        """往期回顾：一篇作品只取最新那条带链接的记录，避免同一篇重复刷屏。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT p.work_id, w.title, p.url, p.published_at "
                "FROM publications p JOIN works w ON w.id = p.work_id "
                "WHERE p.id = (SELECT p2.id FROM publications p2 "
                "  WHERE p2.work_id = p.work_id AND p2.url <> '' "
                "  ORDER BY p2.published_at DESC, p2.created_at DESC LIMIT 1) "
                "AND p.work_id <> ? AND (? = '' OR w.subtype = ?) "
                "ORDER BY p.published_at DESC LIMIT ?",
                (exclude_work_id or "", subtype or "", subtype or "",
                 max(1, min(int(limit), 20))),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ==================== 选题池与写作统计 ====================

    async def add_idea(self, text: str) -> dict[str, Any]:
        assert self._db is not None
        iid = _new_id("idea_")
        now = _now()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO ideas(id, text, status, work_id, created_at, updated_at) "
                "VALUES(?,?,'open','',?,?)",
                (iid, text, now, now),
            )
            await self._db.commit()
            cur = await self._db.execute("SELECT * FROM ideas WHERE id = ?", (iid,))
            row = await cur.fetchone()
        return dict(row) if row else {}

    async def get_idea(self, idea_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_ideas(self, *, status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
        assert self._db is not None
        sql = "SELECT * FROM ideas"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, _MAX_LIMIT)))
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_idea(self, idea_id: str, **fields: Any) -> bool:
        allowed = {"text", "status", "work_id"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in updates)
        async with self._lock:
            cur = await self._db.execute(
                f"UPDATE ideas SET {sets}, updated_at = ? WHERE id = ?",
                [*updates.values(), _now(), idea_id],
            )
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def delete_idea(self, idea_id: str) -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
            await self._db.commit()
        return (cur.rowcount or 0) > 0

    async def _log_writes_locked(self, now: float, chars: int) -> None:
        """把一次手写保存记到当天。**调用方必须已持有 self._lock**。"""
        assert self._db is not None
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        await self._db.execute(
            "INSERT INTO write_days(day, chars, saves, updated_at) VALUES(?,?,1,?) "
            "ON CONFLICT(day) DO UPDATE SET chars = chars + excluded.chars, "
            "saves = saves + 1, updated_at = excluded.updated_at",
            (day, max(0, int(chars)), now),
        )

    async def daily_writes(self, *, days: int = 30) -> list[dict[str, Any]]:
        """按天的手写净增字数（`update_node` 实时累加，不在查询时扫版本表）。

        负增量记 0：删改不该显示成「写了负字」；只删不改的那一天仍会留一行（chars=0），
        连续天数按 chars>0 判，自然不算写作日。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT day, chars, saves FROM write_days ORDER BY day DESC LIMIT ?",
                (max(1, min(days, 180)),),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

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

    async def resolve_issue(self, issue_id: str, note: str = "") -> bool:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE issues SET resolved = 1, resolution_note = ? WHERE id = ?",
                (note or "", issue_id),
            )
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

    async def search_chunks_global(
        self, match_expr: str, *, kinds: tuple[str, ...] = ("body", "source"), limit: int = 40,
    ) -> list[dict[str, Any]]:
        """跨作品检索：与 `search_chunks` 同一套 FTS，只是不按 work_id 过滤。

        作品一多，「我之前在哪篇写过这个」就只能一篇篇点进去搜——那是书架最先崩的一步。
        """
        assert self._db is not None
        if not match_expr:
            return []
        cap = max(1, min(limit, 120))
        async with self._lock:
            cur = await self._db.execute(
                "SELECT chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts "
                "WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                (match_expr, cap * 3),
            )
            hits = await cur.fetchall()
            if not hits:
                return []
            order = {r["chunk_id"]: float(r["rank"] or 0.0) for r in hits}
            marks = ",".join("?" for _ in hits)
            kms = ",".join("?" for _ in kinds)
            cur = await self._db.execute(
                "SELECT c.id, c.work_id, c.source_kind, c.node_id, c.source_id, c.ordinal, "
                "c.text, w.title AS work_title, w.status AS work_status, n.title AS node_title "
                "FROM chunks c JOIN works w ON w.id = c.work_id "
                "LEFT JOIN nodes n ON n.id = c.node_id "
                f"WHERE c.id IN ({marks}) AND c.source_kind IN ({kms})",
                [*order.keys(), *kinds],
            )
            rows = await cur.fetchall()
        out = [dict(r) for r in rows]
        for d in out:
            d["score"] = -order.get(d["id"], 0.0)
        # 保持 FTS 的排序（IN 查询不保证顺序）
        out.sort(key=lambda d: order.get(d["id"], 0.0))
        return out[:cap]

    async def search_works_meta(self, q: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """标题/主旨里直接命中。

        正文 FTS 只索引 chunks，搜不到书名；而人找东西常常只记得名字。
        """
        assert self._db is not None
        if not (q or "").strip():
            return []
        like = f"%{q.strip()}%"
        async with self._lock:
            cur = await self._db.execute(
                "SELECT id, title, premise, kind, status, updated_at FROM works "
                "WHERE title LIKE ? OR premise LIKE ? ORDER BY updated_at DESC LIMIT ?",
                (like, like, max(1, min(limit, 20))),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

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

    async def update_entity(
        self, entity_id: str, *, name: Optional[str] = None, kind: Optional[str] = None,
        canonical: Optional[str] = None, aliases: Optional[list[str]] = None,
        status: Optional[str] = None, status_ordinal: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """作者直接改图谱里的实体：改名、改类型、补别名、改状态。

        抽取器只按 canonical 保守对齐，作者却知道「阿昭就是林昭」——不改的话
        每次抽取都会把同一个人再拆一个节点出来。canonical 冲突时抛 ValueError，
        由路由落成 400，不静默合并两个实体。
        """
        assert self._db is not None
        existing = await self.get_entity(entity_id)
        if not existing:
            return None
        updates: dict[str, Any] = {}
        if name is not None and name.strip():
            updates["name"] = name.strip()
        if kind is not None:
            updates["kind"] = kind
        if canonical is not None and canonical.strip():
            updates["canonical"] = canonical.strip()
        if aliases is not None:
            updates["aliases"] = json.dumps(
                [a.strip() for a in aliases if a and a.strip()], ensure_ascii=False)
        if status is not None:
            updates["status"] = status
            updates["status_ordinal"] = int(status_ordinal or 0)
        elif status_ordinal is not None:
            updates["status_ordinal"] = int(status_ordinal)
        if not updates:
            return existing

        async with self._lock:
            new_canonical = updates.get("canonical")
            if new_canonical and new_canonical != existing.get("canonical"):
                cur = await self._db.execute(
                    "SELECT id FROM entities WHERE work_id = ? AND canonical = ? AND id <> ?",
                    (existing["work_id"], new_canonical, entity_id),
                )
                if await cur.fetchone():
                    raise ValueError(f"已经有同名实体：{new_canonical}")
            sets = ", ".join(f"{k} = ?" for k in updates)
            await self._db.execute(
                f"UPDATE entities SET {sets} WHERE id = ?",
                list(updates.values()) + [entity_id],
            )
            # 实体挂着作者手写的设定卡时，改名要跟着改卡名：卡是抽取时的
            # 权威提示，两边名字不一致，下一次抽取会把旧名再当成一个人。
            if "name" in updates and existing.get("card_id"):
                await self._db.execute(
                    "UPDATE cards SET name = ?, updated_at = ? WHERE id = ?",
                    (updates["name"], _now(), existing["card_id"]),
                )
            await self._db.commit()
        return await self.get_entity(entity_id)

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

    async def get_foreshadow(self, foreshadow_id: str) -> Optional[dict[str, Any]]:
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM foreshadows WHERE id = ?", (foreshadow_id,))
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_foreshadow(
        self, foreshadow_id: str, *, status: str, ordinal: int = 0,
    ) -> Optional[dict[str, Any]]:
        """作者手动回收 / 弃用伏笔：抽出来的伏笔不一定都算数。"""
        if status not in ("open", "resolved", "abandoned"):
            raise ValueError(f"伏笔状态不认：{status}")
        assert self._db is not None
        async with self._lock:
            if status == "resolved":
                cur = await self._db.execute(
                    "UPDATE foreshadows SET status = ?, resolved_ordinal = ? WHERE id = ?",
                    (status, int(ordinal), foreshadow_id),
                )
            elif status == "open":
                cur = await self._db.execute(
                    "UPDATE foreshadows SET status = 'open', resolved_ordinal = NULL WHERE id = ?",
                    (foreshadow_id,),
                )
            else:
                cur = await self._db.execute(
                    "UPDATE foreshadows SET status = ?, resolved_ordinal = NULL WHERE id = ?",
                    (status, foreshadow_id),
                )
            await self._db.commit()
        if not (cur.rowcount or 0):
            return None
        return await self.get_foreshadow(foreshadow_id)

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

    async def _mark_index_dirty_locked(self, work_id: str) -> None:
        """把库标脏：内容没变、但结构变了的那些改动（重排、找回）走这里。

        **调用方必须已持有 self._lock**。不重算索引与摘要（正文哈希没变，重算就是白烧调用），
        只是让下一次重建知道「这一本的顺序/成员动过了」。
        """
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO index_state(work_id, dirty, updated_at) VALUES(?,1,?) "
            "ON CONFLICT(work_id) DO UPDATE SET dirty = 1, updated_at = excluded.updated_at",
            (work_id, _now()),
        )

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
            # 末尾带上 jid：这一列有 UNIQUE，只精到毫秒的话「同一毫秒入两笔」就会撞键。
            # 入队都不带 idem_key 时会退化成 "task_type#毫秒"，两笔完全相等；
            # 带业务键的生产入队撞不上，但不能让后面的调用方重新踩回这个坑。
            unique_key = f"{idem_key or task_type}#{int(now * 1000)}#{jid}"
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
                        limit: int = 100, newest: bool = False) -> list[dict[str, Any]]:
        """任务列表。

        默认按队列顺序（priority 高的先、同优先级按入队时间）——那是 worker 领活的顺序；
        `newest=True` 换成按更新时间倒序，给控制台的「刚刚发生了什么」用：
        只拿前 N 条队列顺序的话，任务一多，最新的反而永远看不到。
        """
        assert self._db is not None
        sql = "SELECT * FROM jobs WHERE 1 = 1"
        params: list[Any] = []
        if work_id:
            sql += " AND work_id = ?"
            params.append(work_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if newest:
            sql += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        else:
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

    async def run_baselines(
        self, work_id: str, task_type: str = "", sample: int = 200
    ) -> dict[str, Any]:
        """这部作品历史成功 run 的字数中位数：批量前成本预估的依据。

        只算 status='ok'：失败 run 的输出常是半截或废稿，拿它们拉中位数会低估。
        prompt_chars 是开跑前就能定下来的量，预估吃这个最稳。

        task_type 缺省时算「写正文」一档的两个名字：pipeline 落 article_draft、
        队列 worker 落 draft——小说批量跑得再多，只查前者就永远取不到历史，
        闸门判的就是一份假数（审查 M1）。显式给了就只查那一个。
        """
        assert self._db is not None
        if task_type:
            where = "task_type = ?"
            params: list[Any] = [work_id, task_type, max(1, min(sample, _MAX_LIMIT))]
        else:
            where = "task_type IN ('article_draft', 'draft')"
            params = [work_id, max(1, min(sample, _MAX_LIMIT))]
        async with self._lock:
            cur = await self._db.execute(
                "SELECT prompt_chars, output_chars FROM runs "
                f"WHERE work_id = ? AND {where} AND status = 'ok' "
                "ORDER BY started_at DESC LIMIT ?",
                tuple(params),
            )
            rows = await cur.fetchall()
        from app.studio.cost import median

        return {
            "count": len(rows),
            "prompt_median": median([int(r[0] or 0) for r in rows]),
            "output_median": median([int(r[1] or 0) for r in rows]),
        }

    async def output_chars_total(self, work_id: str) -> int:
        """这部作品历史上所有成功 run 的产出字数合计。

        不筛 task_type：预算是「这本书一共烧了多少钱」，摘要/抽取/检查
        走的也是同一个计费通道，都是真金（审查 m10 的账本依据）。
        只算 status='ok'：失败 run 没结算，按它扣预算会凭空压低可用额度。
        """
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute(
                "SELECT COALESCE(SUM(output_chars), 0) FROM runs "
                "WHERE work_id = ? AND status = 'ok'",
                (work_id,),
            )
            row = await cur.fetchone()
        return int(row[0] or 0)

    # ==================== 整本搬迁（作品包导出 / 导入） ====================

    async def export_work(self, work_id: str) -> Optional[dict[str, Any]]:
        """把一本书连同树、历史版本与图谱打成一份自足的 JSON 包。

        不搬 chunks / summaries / runs：那些都是从正文派生的，导回去重建一次
        比原样搬更省、也更准——旧库里那份索引可能本来就过期了。
        """
        assert self._db is not None

        async def rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            cur = await self._db.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]

        async with self._lock:
            cur = await self._db.execute("SELECT * FROM works WHERE id = ?", (work_id,))
            work = await cur.fetchone()
            if not work:
                return None
            nodes = await rows(
                "SELECT id, parent_id, path, depth, ordinal, kind, title, content, status, "
                "word_count, content_hash, summary, plan_json, created_at, updated_at "
                "FROM nodes WHERE work_id = ? ORDER BY path", (work_id,),
            )
            versions = await rows(
                "SELECT v.node_id, v.version_no, v.content, v.source, v.created_at "
                "FROM node_versions v JOIN nodes n ON n.id = v.node_id "
                "WHERE n.work_id = ? ORDER BY v.node_id, v.version_no", (work_id,),
            )
            sources = await rows(
                "SELECT id, kind, title, url, content, published_at, fetched_at, used, created_at "
                "FROM sources WHERE work_id = ? ORDER BY created_at", (work_id,),
            )
            facts = await rows(
                "SELECT id, statement, source_id, confidence, valid_until, cited_node_id, created_at "
                "FROM facts WHERE work_id = ? ORDER BY created_at", (work_id,),
            )
            terms = await rows(
                "SELECT id, term, definition, preferred, forbidden, created_at "
                "FROM terms WHERE work_id = ?", (work_id,),
            )
            cards = await rows(
                "SELECT id, kind, name, aliases, summary, data_json, pinned, updated_at "
                "FROM cards WHERE work_id = ?", (work_id,),
            )
            entities = await rows(
                "SELECT id, kind, name, canonical, aliases, card_id, first_ordinal, status, "
                "status_ordinal FROM entities WHERE work_id = ?", (work_id,),
            )
            relations = await rows(
                "SELECT id, src_id, dst_id, rel_type, since_ordinal, until_ordinal, confidence, "
                "evidence_ordinal FROM relations WHERE work_id = ?", (work_id,),
            )
            events = await rows(
                "SELECT id, ordinal, seq_in_unit, summary, participants, location_id, story_time, "
                "importance FROM events WHERE work_id = ? ORDER BY ordinal, seq_in_unit",
                (work_id,),
            )
            states = await rows(
                "SELECT id, entity_id, ordinal, state_json FROM entity_states WHERE work_id = ? "
                "ORDER BY ordinal", (work_id,),
            )
            foreshadows = await rows(
                "SELECT id, planted_ordinal, planned_payoff_ordinal, resolved_ordinal, content, "
                "status, importance FROM foreshadows WHERE work_id = ?", (work_id,),
            )

        return {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "exported_at": _now(),
            "work": dict(work),
            "nodes": nodes,
            "versions": versions,
            "sources": sources,
            "facts": facts,
            "terms": terms,
            "cards": cards,
            "entities": entities,
            "relations": relations,
            "events": events,
            "states": states,
            "foreshadows": foreshadows,
        }

    async def export_library(self) -> dict[str, Any]:
        """整库导出：每本书一个包，外面套一层书目。换机时导这一个就够。"""
        assert self._db is not None
        async with self._lock:
            cur = await self._db.execute("SELECT id FROM works ORDER BY updated_at DESC")
            ids = [r["id"] for r in await cur.fetchall()]
        works: list[dict[str, Any]] = []
        for wid in ids[:_MAX_LIBRARY_WORKS]:
            bundle = await self.export_work(wid)
            if bundle:
                works.append(bundle)
        return {
            "format": LIBRARY_FORMAT,
            "version": BUNDLE_VERSION,
            "exported_at": _now(),
            "works": works,
        }

    async def import_work(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """把一份作品包落成一本新书：id 全部重发，引用跟着改。

        只做「新增」，不做「按原 id 覆盖」：迁移与复制是同一件事，覆盖是另一件
        （半覆盖比不覆盖更难收拾）。chunks 不从包里搬，由路由重建索引。
        """
        assert self._db is not None
        if not isinstance(bundle, dict):
            raise ValueError("包不是 JSON 对象")
        if bundle.get("format") != BUNDLE_FORMAT:
            raise ValueError("不是作品包：format 对不上")
        if _bundle_int(bundle.get("version")) != BUNDLE_VERSION:
            raise ValueError("包版本不认识，请用同版本导出")
        work = bundle.get("work")
        if not isinstance(work, dict):
            raise ValueError("包里没有 work")
        title = _bundle_text(work.get("title")).strip()
        if not title:
            raise ValueError("作品没有标题")

        raw_nodes = [n for n in (bundle.get("nodes") or []) if isinstance(n, dict)]
        if len(raw_nodes) > _MAX_BUNDLE_NODES:
            raise ValueError(f"节点太多（{len(raw_nodes)}），上限 {_MAX_BUNDLE_NODES}")
        kept_nodes: list[dict[str, Any]] = []
        node_map: dict[str, str] = {}
        node_paths: dict[str, str] = {}
        paths: set[str] = set()
        for n in raw_nodes:
            old = _bundle_text(n.get("id"))
            if not old or old in node_map or not _bundle_text(n.get("title")).strip():
                continue
            path = _bundle_text(n.get("path")).strip() or f"{len(node_map) + 1:04d}"
            if path in paths:
                raise ValueError(f"节点 path 重复：{path}")
            paths.add(path)
            node_map[old] = _new_id("n_")
            node_paths[old] = path
            kept_nodes.append(n)

        wid = _new_id("w_")
        now = _now()
        kind = _bundle_text(work.get("kind"))
        if kind not in ("novel", "article"):
            kind = "article"
        w_status = _bundle_text(work.get("status")) or "active"
        if w_status not in ("active", "paused", "archived"):
            w_status = "active"
        template_id = _bundle_text(work.get("template_id")) or None

        source_map: dict[str, str] = {}
        card_map: dict[str, str] = {}
        entity_map: dict[str, str] = {}
        counts = {"nodes": 0, "versions": 0, "sources": 0, "entities": 0, "events": 0}

        async with self._lock:
            try:
                if template_id:
                    cur = await self._db.execute(
                        "SELECT 1 FROM templates WHERE id = ?", (template_id,)
                    )
                    if not await cur.fetchone():
                        template_id = None
                await self._db.execute(
                    "INSERT INTO works(id, kind, scale, subtype, title, premise, status, template_id, "
                    "target_words, style_json, meta_json, digest, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (wid, kind, _bundle_text(work.get("scale")) or "single",
                     _bundle_text(work.get("subtype")), title[:120],
                     _bundle_text(work.get("premise")), w_status, template_id,
                     _bundle_int(work.get("target_words")),
                     _bundle_text(work.get("style_json")) or "{}",
                     _bundle_text(work.get("meta_json")) or "{}",
                     _bundle_text(work.get("digest")), now, now),
                )
                await self._db.execute(
                    "INSERT INTO index_state(work_id, updated_at) VALUES(?,?)", (wid, now)
                )

                # ---- 节点树：path 原样保留，parent_id 换成本库新 id ----
                for n in kept_nodes:
                    old = _bundle_text(n["id"])
                    content = _bundle_text(n.get("content"))
                    kind_n = _bundle_text(n.get("kind"))
                    if kind_n not in ("volume", "chapter", "section"):
                        kind_n = "section"
                    ordinal = n.get("ordinal")
                    created = _bundle_float(n.get("created_at"), now) or now
                    updated = _bundle_float(n.get("updated_at"), created) or created
                    # 计划字段可能是旧包里的空串或坏 JSON；坏掉就退化成空计划，
                    # 不能因为一条节点计划把整本书导不进来。
                    try:
                        plan = json.loads(_bundle_text(n.get("plan_json")) or "{}")
                    except json.JSONDecodeError:
                        plan = {}
                    if not isinstance(plan, dict):
                        plan = {}
                    await self._db.execute(
                        "INSERT INTO nodes(id, work_id, parent_id, path, depth, ordinal, kind, title, "
                        "content, status, word_count, content_hash, summary, plan_json, "
                        "created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (node_map[old], wid, node_map.get(_bundle_text(n.get("parent_id"))),
                         node_paths[old], _bundle_int(n.get("depth")),
                         None if ordinal is None else _bundle_int(ordinal), kind_n,
                         _bundle_text(n.get("title"))[:120], content,
                         _bundle_text(n.get("status")) or "draft",
                         _count_words(content), _hash(content) if content else "",
                         _bundle_text(n.get("summary")),
                         json.dumps(plan, ensure_ascii=False), created, updated),
                    )
                    counts["nodes"] += 1

                # ---- 历史版本：内容照搬，run_id 不搬（runs 不在包里）----
                seen_versions: set[tuple[str, int]] = set()
                for v in (bundle.get("versions") or []):
                    if not isinstance(v, dict):
                        continue
                    nid = node_map.get(_bundle_text(v.get("node_id")))
                    no = _bundle_int(v.get("version_no"))
                    if not nid or no <= 0 or (nid, no) in seen_versions:
                        continue
                    seen_versions.add((nid, no))
                    src = _bundle_text(v.get("source"))
                    if src not in BUNDLE_SOURCES:
                        src = "human"
                    await self._db.execute(
                        "INSERT INTO node_versions(id, node_id, version_no, content, source, run_id, "
                        "created_at) VALUES(?,?,?,?,?,NULL,?)",
                        (_new_id("v_"), nid, no, _bundle_text(v.get("content")), src,
                         _bundle_float(v.get("created_at"), now) or now),
                    )
                    counts["versions"] += 1

                # ---- 素材 / 事实 / 术语 ----
                for s in (bundle.get("sources") or []):
                    if not isinstance(s, dict):
                        continue
                    old = _bundle_text(s.get("id"))
                    if not old or old in source_map:
                        continue
                    source_map[old] = _new_id("s_")
                    await self._db.execute(
                        "INSERT INTO sources(id, work_id, kind, title, url, content, published_at, "
                        "fetched_at, used, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (source_map[old], wid, _bundle_text(s.get("kind")) or "note",
                         _bundle_text(s.get("title")), _bundle_text(s.get("url")),
                         _bundle_text(s.get("content")), _bundle_text(s.get("published_at")),
                         _bundle_float(s.get("fetched_at"), 0) or None,
                         _bundle_int(s.get("used")), _bundle_float(s.get("created_at"), now) or now),
                    )
                    counts["sources"] += 1

                for item in (bundle.get("facts") or []):
                    if not isinstance(item, dict):
                        continue
                    statement = _bundle_text(item.get("statement")).strip()
                    if not statement:
                        continue
                    await self._db.execute(
                        "INSERT INTO facts(id, work_id, statement, source_id, confidence, valid_until, "
                        "cited_node_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (_new_id("f_"), wid, statement,
                         source_map.get(_bundle_text(item.get("source_id"))),
                         _bundle_float(item.get("confidence"), 1.0),
                         _bundle_text(item.get("valid_until")),
                         node_map.get(_bundle_text(item.get("cited_node_id"))),
                         _bundle_float(item.get("created_at"), now) or now),
                    )

                seen_terms: set[str] = set()
                for t in (bundle.get("terms") or []):
                    if not isinstance(t, dict):
                        continue
                    term = _bundle_text(t.get("term")).strip()
                    if not term or term in seen_terms:
                        continue
                    seen_terms.add(term)
                    await self._db.execute(
                        "INSERT INTO terms(id, work_id, term, definition, preferred, forbidden, "
                        "created_at) VALUES(?,?,?,?,?,?,?)",
                        (_new_id("t_"), wid, term, _bundle_text(t.get("definition")),
                         _bundle_text(t.get("preferred")),
                         _bundle_text(t.get("forbidden")) or "[]",
                         _bundle_float(t.get("created_at"), now) or now),
                    )

                # ---- 图谱：卡片 → 实体 → 关系 / 事件 / 状态 / 伏笔 ----
                for c in (bundle.get("cards") or []):
                    if not isinstance(c, dict):
                        continue
                    old = _bundle_text(c.get("id"))
                    if not old or old in card_map:
                        continue
                    card_map[old] = _new_id("card_")
                    await self._db.execute(
                        "INSERT INTO cards(id, work_id, kind, name, aliases, summary, data_json, "
                        "pinned, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (card_map[old], wid, _bundle_text(c.get("kind")) or "character",
                         _bundle_text(c.get("name")) or "未命名",
                         _bundle_text(c.get("aliases")) or "[]",
                         _bundle_text(c.get("summary")),
                         _bundle_text(c.get("data_json")) or "{}",
                         _bundle_int(c.get("pinned")),
                         _bundle_float(c.get("updated_at"), now) or now),
                    )

                seen_canon: set[str] = set()
                for e in (bundle.get("entities") or []):
                    if not isinstance(e, dict):
                        continue
                    old = _bundle_text(e.get("id"))
                    canonical = _bundle_text(e.get("canonical")).strip()
                    if not old or old in entity_map or not canonical or canonical in seen_canon:
                        continue
                    seen_canon.add(canonical)
                    entity_map[old] = _new_id("e_")
                    await self._db.execute(
                        "INSERT INTO entities(id, work_id, kind, name, canonical, aliases, card_id, "
                        "first_ordinal, status, status_ordinal) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (entity_map[old], wid, _bundle_text(e.get("kind")) or "character",
                         _bundle_text(e.get("name")) or canonical, canonical,
                         _bundle_text(e.get("aliases")) or "[]",
                         card_map.get(_bundle_text(e.get("card_id"))),
                         _bundle_int(e.get("first_ordinal")),
                         _bundle_text(e.get("status")) or "alive",
                         _bundle_int(e.get("status_ordinal"))),
                    )
                    counts["entities"] += 1

                for r in (bundle.get("relations") or []):
                    if not isinstance(r, dict):
                        continue
                    src = entity_map.get(_bundle_text(r.get("src_id")))
                    dst = entity_map.get(_bundle_text(r.get("dst_id")))
                    if not src or not dst:
                        continue
                    await self._db.execute(
                        "INSERT OR IGNORE INTO relations(id, work_id, src_id, dst_id, rel_type, "
                        "since_ordinal, until_ordinal, confidence, evidence_ordinal) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (_new_id("r_"), wid, src, dst, _bundle_text(r.get("rel_type")) or "关系",
                         _bundle_int(r.get("since_ordinal")),
                         None if r.get("until_ordinal") is None else _bundle_int(r.get("until_ordinal")),
                         _bundle_float(r.get("confidence"), 1.0),
                         None if r.get("evidence_ordinal") is None
                         else _bundle_int(r.get("evidence_ordinal"))),
                    )

                for ev in (bundle.get("events") or []):
                    if not isinstance(ev, dict):
                        continue
                    summary = _bundle_text(ev.get("summary")).strip()
                    if not summary:
                        continue
                    await self._db.execute(
                        "INSERT INTO events(id, work_id, ordinal, seq_in_unit, summary, participants, "
                        "location_id, story_time, importance) VALUES(?,?,?,?,?,?,?,?,?)",
                        (_new_id("ev_"), wid, _bundle_int(ev.get("ordinal")),
                         _bundle_int(ev.get("seq_in_unit")), summary,
                         _map_participants(ev.get("participants"), entity_map),
                         entity_map.get(_bundle_text(ev.get("location_id"))),
                         _bundle_text(ev.get("story_time")),
                         _bundle_int(ev.get("importance"), 1)),
                    )
                    counts["events"] += 1

                seen_states: set[tuple[str, int]] = set()
                for st in (bundle.get("states") or []):
                    if not isinstance(st, dict):
                        continue
                    eid = entity_map.get(_bundle_text(st.get("entity_id")))
                    ordinal = _bundle_int(st.get("ordinal"))
                    if not eid or (eid, ordinal) in seen_states:
                        continue
                    seen_states.add((eid, ordinal))
                    await self._db.execute(
                        "INSERT INTO entity_states(id, work_id, entity_id, ordinal, state_json) "
                        "VALUES(?,?,?,?,?)",
                        (_new_id("st_"), wid, eid, ordinal,
                         _bundle_text(st.get("state_json")) or "{}"),
                    )

                for fs in (bundle.get("foreshadows") or []):
                    if not isinstance(fs, dict):
                        continue
                    content = _bundle_text(fs.get("content")).strip()
                    if not content:
                        continue
                    await self._db.execute(
                        "INSERT INTO foreshadows(id, work_id, planted_ordinal, planned_payoff_ordinal, "
                        "resolved_ordinal, content, status, importance) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (_new_id("fs_"), wid, _bundle_int(fs.get("planted_ordinal")),
                         None if fs.get("planned_payoff_ordinal") is None
                         else _bundle_int(fs.get("planned_payoff_ordinal")),
                         None if fs.get("resolved_ordinal") is None
                         else _bundle_int(fs.get("resolved_ordinal")),
                         content, _bundle_text(fs.get("status")) or "open",
                         _bundle_int(fs.get("importance"), 1)),
                    )

                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

        return {"id": wid, "title": title, **counts}

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

    async def cost_quality(self, work_limit: int = 50, node_limit: int = 50) -> dict[str, Any]:
        """生成账本：runs 的耗时/字数按作品与按节汇总，再加未处理硬告警。

        只统计真的跑过的作品与节——没调用过模型的书不进成本表。
        「质量」只看两样落过库的：跑批打分（runs.score）与未处理 error 级
        告警（warn/info 是提示，不算错）。没打分的跑批不按 0 分算，直接不计入
        均分——把「没评」混进「差评」会把均分拉得毫无意义。
        """
        assert self._db is not None
        wl = max(1, min(work_limit, 100))
        nl = max(1, min(node_limit, 200))
        agg = (
            "COUNT(*) AS runs, "
            "SUM(CASE WHEN r.status='ok' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN r.status='interrupted' THEN 1 ELSE 0 END) AS interrupted, "
            "SUM(CASE WHEN r.status='running' THEN 1 ELSE 0 END) AS running, "
            "COALESCE(SUM(r.elapsed_ms),0) AS elapsed_ms, "
            "COALESCE(SUM(r.prompt_chars),0) AS prompt_chars, "
            "COALESCE(SUM(r.output_chars),0) AS output_chars, "
            "COUNT(r.score) AS scored, AVG(r.score) AS avg_score"
        )

        def shape(row: Any) -> dict[str, Any]:
            d = dict(row)
            for k in ("runs", "ok", "failed", "interrupted", "running", "elapsed_ms",
                      "prompt_chars", "output_chars", "scored"):
                d[k] = int(d.get(k) or 0)
            d["done"] = d["ok"] + d["failed"] + d["interrupted"]
            d["avg_elapsed_ms"] = (
                int(round(d["elapsed_ms"] / d["done"])) if d["done"] else 0
            )
            avg = d.get("avg_score")
            d["avg_score"] = round(float(avg), 2) if avg is not None else None
            return d

        async with self._lock:
            cur = await self._db.execute(
                f"SELECT {agg}, MAX(r.started_at) AS last_at FROM runs r"
            )
            overall = shape(await cur.fetchone())

            cur = await self._db.execute(
                f"SELECT r.task_type, {agg} FROM runs r GROUP BY r.task_type "
                "ORDER BY elapsed_ms DESC"
            )
            by_task = [shape(r) for r in await cur.fetchall()]

            cur = await self._db.execute(
                f"SELECT r.work_id, w.title AS work_title, {agg}, MAX(r.started_at) AS last_at "
                "FROM runs r LEFT JOIN works w ON w.id = r.work_id "
                "GROUP BY r.work_id ORDER BY elapsed_ms DESC LIMIT ?",
                (wl,),
            )
            works = [shape(r) for r in await cur.fetchall()]

            cur = await self._db.execute(
                "SELECT work_id, COUNT(*) AS c FROM issues "
                "WHERE resolved = 0 AND severity = 'error' GROUP BY work_id"
            )
            work_errors = {r["work_id"]: int(r["c"]) for r in await cur.fetchall()}
            cur = await self._db.execute(
                "SELECT node_id, COUNT(*) AS c FROM issues "
                "WHERE resolved = 0 AND severity = 'error' AND node_id IS NOT NULL "
                "GROUP BY node_id"
            )
            node_errors = {r["node_id"]: int(r["c"]) for r in await cur.fetchall()}

            cur = await self._db.execute(
                f"SELECT r.work_id, w.title AS work_title, r.node_id, n.title AS node_title, "
                f"n.path AS node_path, {agg} "
                "FROM runs r LEFT JOIN nodes n ON n.id = r.node_id "
                "LEFT JOIN works w ON w.id = r.work_id "
                "WHERE r.node_id IS NOT NULL AND r.node_id != '' "
                "GROUP BY r.work_id, r.node_id ORDER BY elapsed_ms DESC LIMIT ?",
                (nl,),
            )
            nodes = [shape(r) for r in await cur.fetchall()]

        for w in works:
            w["open_errors"] = work_errors.get(w["work_id"], 0)
        for n in nodes:
            n["open_errors"] = node_errors.get(n["node_id"], 0)
        return {
            "overall": overall,
            "by_task": by_task,
            "works": works,
            "nodes": nodes,
            "limits": {"works": wl, "nodes": nl},
        }
