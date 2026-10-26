"""知识图谱：抽取、合并、子图查询。

两条原则决定了这里的实现：

1. **抽取必须能「认出老人」**。prompt 里带上已有实体清单与作者设定卡，
   否则同一个人会被拆成多个节点（「林昭」「林公子」「阿昭」），
   后面所有一致性检查都建在一张错误的图上。对齐靠 canonical，
   而 canonical 只做保守归一（去空白），不做激进的同义词合并——宁可漏合，不可错合。

2. **子图查询走 Python 侧 BFS**，不用 SQL 递归 CTE。单书实体 ≤800、关系几千条，
   一次载入内存比递归查询快一个量级，也更容易按 confidence × 重要度排序。
   缓存以 work_id 为键，抽取/改卡时显式失效——只有这两条路径能改动图。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.studio.jsonutil import parse_json_loose

# 类型白名单：不引更多。类型膨胀会让抽取准确率断崖下跌。
ENTITY_KINDS = ("character", "location", "item", "faction", "rule")
REL_TYPES = ("师徒", "亲属", "敌对", "同盟", "隶属", "持有", "位于", "参与", "因果")
STATUSES = ("alive", "dead", "destroyed", "missing")

KIND_LABEL = {
    "character": "人物", "location": "地点", "item": "物品",
    "faction": "势力", "rule": "规则", "event": "事件", "foreshadow": "伏笔",
}
STATUS_LABEL = {
    "alive": "存活", "dead": "已死亡", "destroyed": "已毁", "missing": "下落不明",
}

MAX_INPUT_CHARS = 8000
MAX_KNOWN = 120

EXTRACT_SYSTEM = (
    "你是小说设定提取器。你只输出合法 JSON，不输出解释、前言或 Markdown 代码块。"
    "只提取正文明确写出的事实；推测与暗示不要写进事件，未点明的伏笔不要凭空补。"
)

EXTRACT_USER = """读下面这一节正文，提取结构化设定。

已有实体（同一个事物必须复用这里的名字，不要另造称呼）：
{known}

作者设定卡（权威；与正文冲突时以卡片为准，并在 states 里写出变化）：
{cards}

严格按下面的 JSON 输出，字段一个都不能少，没有就给空数组：
{{
  "entities": [{{"name": "", "kind": "character|location|item|faction|rule", "aliases": [""]}}],
  "relations": [{{"src": "", "dst": "", "type": "师徒|亲属|敌对|同盟|隶属|持有|位于|参与|因果"}}],
  "events": [{{"summary": "", "participants": [""], "location": "", "story_time": "", "importance": 1}}],
  "states": [{{"entity": "", "status": "alive|dead|destroyed|missing", "note": "", "location": "", "owner": ""}}],
  "foreshadows": [{{"content": "", "importance": 1}}],
  "resolved_foreshadows": ["被本节明确回收的伏笔原文"]
}}

规则：
- participants / location / states.entity 用「已有实体」里的名字；新出现的人物直接写全名
- importance 取 1-3，只有推动主线的才给 3
- 人物状态没有变化时，states 留空数组；死亡、受伤、失物、位置迁移才算变化
- events 只写本节真正发生的事，不要把计划与猜测写成事件

<section title="{title}">
{content}
</section>"""


# ==================== 抽取结果模型 ====================

def _dicts(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def _strs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if isinstance(x, (str, int, float)) and str(x).strip()]


class EntityOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    kind: str = "character"
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name", "kind", mode="before")
    @classmethod
    def _as_str(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("aliases", mode="before")
    @classmethod
    def _as_strs(cls, v: Any) -> list[str]:
        return _strs(v)


class RelationOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    src: str = ""
    dst: str = ""
    type: str = ""

    @field_validator("src", "dst", "type", mode="before")
    @classmethod
    def _as_str(cls, v: Any) -> str:
        return str(v or "").strip()


class EventOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = ""
    participants: list[str] = Field(default_factory=list)
    location: str = ""
    story_time: str = ""
    importance: int = 1

    @field_validator("summary", "location", "story_time", mode="before")
    @classmethod
    def _as_str(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("participants", mode="before")
    @classmethod
    def _as_strs(cls, v: Any) -> list[str]:
        return _strs(v)

    @field_validator("importance", mode="before")
    @classmethod
    def _as_int(cls, v: Any) -> int:
        try:
            return max(1, min(3, int(v)))
        except (TypeError, ValueError):
            return 1


class StateOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entity: str = ""
    status: str = ""
    note: str = ""
    location: str = ""
    owner: str = ""

    @field_validator("entity", "status", "note", "location", "owner", mode="before")
    @classmethod
    def _as_str(cls, v: Any) -> str:
        return str(v or "").strip()


class ForeshadowOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str = ""
    importance: int = 1

    @field_validator("content", mode="before")
    @classmethod
    def _as_str(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("importance", mode="before")
    @classmethod
    def _as_int(cls, v: Any) -> int:
        try:
            return max(1, min(3, int(v)))
        except (TypeError, ValueError):
            return 1


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entities: list[EntityOut] = Field(default_factory=list)
    relations: list[RelationOut] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)
    states: list[StateOut] = Field(default_factory=list)
    foreshadows: list[ForeshadowOut] = Field(default_factory=list)
    resolved_foreshadows: list[str] = Field(default_factory=list)

    @field_validator("entities", "relations", "events", "states", "foreshadows", mode="before")
    @classmethod
    def _as_dicts(cls, v: Any) -> list[dict]:
        return _dicts(v)

    @field_validator("resolved_foreshadows", mode="before")
    @classmethod
    def _as_strs(cls, v: Any) -> list[str]:
        return _strs(v)


# ==================== 归一化 ====================

_SPACE = re.compile(r"\s+")


def canon(name: str) -> str:
    """保守归一：只去空白与常见标点。不做同义词合并——错合比漏合更难修。"""
    return _SPACE.sub("", str(name or "")).strip("　·・,，。:：")


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _kind(value: str) -> str:
    v = str(value or "").strip()
    return v if v in ENTITY_KINDS else "character"


def _rel_type(value: str) -> str:
    v = str(value or "").strip()
    return v if v in REL_TYPES else ""


# ==================== 内存图与缓存 ====================

_CACHE: dict[str, dict[str, Any]] = {}


def invalidate(work_id: str) -> None:
    """抽取落库、作者改卡之后必须调用，否则 S6 还在用旧图。"""
    _CACHE.pop(work_id, None)


async def load_graph(store, work_id: str) -> dict[str, Any]:
    """一次性载入整本书的图。几千行，十几毫秒，不值得再拆更细。"""
    if work_id in _CACHE:
        return _CACHE[work_id]
    entities = await store.list_entities(work_id)
    relations = await store.list_relations(work_id)
    foreshadows = await store.list_foreshadows(work_id, status="open")
    cards = await store.list_cards(work_id)
    by_id = {e["id"]: e for e in entities}
    g = {
        "entities": entities,
        "entities_by_id": by_id,
        "relations": relations,
        "foreshadows": foreshadows,
        "cards": cards,
        "by_canon": {e["canonical"]: e for e in entities},
    }
    _CACHE[work_id] = g
    return g


def _mentions(text: str, entities: list[dict], *, limit: int = 12) -> list[dict]:
    """实体链接：正文/提纲里点到了谁。短名容易误伤，要求至少两个字。"""
    hits: list[dict] = []
    if not text:
        return hits
    for e in entities:
        names = [e.get("name") or ""]
        try:
            names += json.loads(e.get("aliases") or "[]")
        except (json.JSONDecodeError, TypeError):
            pass
        for name in names:
            if len(name) < 2:
                continue
            if name in text:
                hits.append(e)
                break
    hits.sort(key=lambda e: (e.get("kind") != "character", int(e.get("first_ordinal") or 0)))
    return hits[:limit]


# ==================== 抽取 ====================

def _known_block(entities: list[dict], cards: list[dict]) -> tuple[str, str]:
    lines: list[str] = []
    for e in entities[:MAX_KNOWN]:
        try:
            aliases = json.loads(e.get("aliases") or "[]")
        except (json.JSONDecodeError, TypeError):
            aliases = []
        extra = f"；别名：{'、'.join(aliases[:3])}" if aliases else ""
        lines.append(f"- {e['name']}（{e.get('kind')}；首见第 {e.get('first_ordinal') or 0} 节{extra}）")
    known = "\n".join(lines) or "（暂无，这是第一节）"

    card_lines = []
    for c in cards[:40]:
        head = f"- {c['name']}（{c.get('kind')}"
        if c.get("summary"):
            head += f"；{c['summary']}"
        head += "）"
        card_lines.append(head)
    return known, ("\n".join(card_lines) or "（暂无）")


async def extract_node(
    gateway, store, work: dict[str, Any], node: dict[str, Any], *,
    model: str, force: bool = False, mode: str = "quick",
) -> dict[str, Any]:
    """抽一节并合并入图。返回统计；跳过时返回 skipped 原因。"""
    import time

    content = (node.get("content") or "").strip()
    if len(content) < 150:
        return {"skipped": "正文太短，不值得抽"}

    stamp = await store.index_stamp("graph", node["id"])
    if stamp == _hash(content) and not force:
        return {"skipped": "内容未变，跳过"}

    g = await load_graph(store, work["id"])
    known, cards_text = _known_block(g["entities"], g["cards"])
    user = EXTRACT_USER.format(
        known=known, cards=cards_text,
        title=node.get("title") or "", content=content[:MAX_INPUT_CHARS],
    )

    parsed: Optional[Extraction] = None
    last_err = ""
    started = time.perf_counter()
    repair = "\n\n上一次输出不是合法 JSON。现在只输出一个合法 JSON 对象，字段一个都不能少，不要解释、不要代码块。"
    for attempt in (1, 2):
        prompt = user if attempt == 1 else user + repair
        try:
            res = await gateway.complete(
                model=model, system=EXTRACT_SYSTEM, prompt=prompt, mode=mode)
        except Exception as exc:  # noqa: BLE001 - 抽不出来不能拖垮写作
            last_err = str(exc)
            logger.warning(f"[graph] 抽取失败 node={node['id']} attempt={attempt}: {exc}")
            continue
        got = parse_json_loose(res.text)
        if not isinstance(got, dict):
            last_err = "不是合法 JSON"
            continue
        try:
            parsed = Extraction.model_validate(got)
        except Exception as exc:  # noqa: BLE001
            last_err = f"结构校验失败：{exc}"
            continue
        break

    if parsed is None:
        return {"error": last_err or "抽取失败"}

    stats = await merge_extraction(store, work["id"], int(node.get("ordinal") or 0), parsed)
    await store.set_index_stamp(ref_kind="graph", ref_id=node["id"], work_id=work["id"],
                                content_hash=_hash(content), chunk_count=stats["entities"])
    await store.touch_index_state(work["id"], last_graph_ordinal=int(node.get("ordinal") or 0))
    invalidate(work["id"])
    stats["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    stats["attempts"] = attempt
    return stats


async def merge_extraction(
    store, work_id: str, ordinal: int, data: Extraction
) -> dict[str, int]:
    """按「对齐 → 补关系 → 记事件 → 快照状态 → 台账伏笔」的顺序合并。"""
    stats = {"entities": 0, "relations": 0, "events": 0, "states": 0,
             "foreshadows": 0, "resolved": 0}

    # 1. 实体：先全部落库，后面才能按名字解析关系
    by_name: dict[str, dict] = {}
    for item in data.entities:
        name = (item.name or "").strip()
        if not name:
            continue
        entity = await store.upsert_entity(
            work_id, kind=_kind(item.kind), name=name, canonical=canon(name),
            aliases=[a for a in item.aliases if a], first_ordinal=ordinal,
        )
        by_name[canon(name)] = entity
        for alias in item.aliases:
            by_name.setdefault(canon(alias), entity)
        stats["entities"] += 1

    # 老实体也要能按名字解析（关系两头常常一个旧一个新）
    for e in await store.list_entities(work_id):
        by_name.setdefault(e["canonical"], e)

    async def resolve(name: str, kind: str = "character") -> Optional[dict]:
        key = canon(name)
        if not key:
            return None
        if key in by_name:
            return by_name[key]
        entity = await store.upsert_entity(
            work_id, kind=_kind(kind), name=str(name).strip(), canonical=key,
            first_ordinal=ordinal,
        )
        by_name[key] = entity
        return entity

    # 2. 关系
    for rel in data.relations:
        rtype = _rel_type(rel.type)
        if not rtype or not rel.src or not rel.dst:
            continue
        src = await resolve(rel.src)
        dst = await resolve(rel.dst, "location" if rtype == "位于" else "character")
        if not src or not dst or src["id"] == dst["id"]:
            continue
        await store.upsert_relation(
            work_id, src_id=src["id"], dst_id=dst["id"], rel_type=rtype,
            since_ordinal=ordinal, evidence_ordinal=ordinal,
        )
        stats["relations"] += 1

    # 3. 事件：先清本节旧的，避免重抽后同一段剧情记两遍
    if data.events:
        await store.clear_events(work_id, ordinal)
    for i, ev in enumerate(data.events):
        summary = (ev.summary or "").strip()
        if not summary:
            continue
        participants: list[str] = []
        for name in ev.participants:
            ent = await resolve(name)
            if ent and ent["id"] not in participants:
                participants.append(ent["id"])
        loc = await resolve(ev.location, "location") if ev.location else None
        await store.add_event(
            work_id, ordinal=ordinal, summary=summary, participants=participants,
            location_id=loc["id"] if loc else None, story_time=ev.story_time,
            importance=ev.importance,
        )
        stats["events"] += 1

    # 4. 状态快照：只在与上一条不同时写，避免每章重复同一份
    for st in data.states:
        if not st.entity:
            continue
        ent = await resolve(st.entity)
        if not ent:
            continue
        payload = {k: v for k, v in
                   (("note", st.note), ("location", st.location), ("owner", st.owner))
                   if v}
        if payload:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            latest = await store.latest_state(ent["id"])
            if not latest or latest.get("state_json") != text:
                await store.add_state(work_id, ent["id"], ordinal, text)
                stats["states"] += 1
        if st.status in STATUSES:
            await store.set_entity_status(ent["id"], st.status, ordinal)
            stats["states"] += 1

    # 5. 伏笔：新增；本节明确回收的直接结掉
    existing = await store.list_foreshadows(work_id, status="open")
    for fs in data.foreshadows:
        text = (fs.content or "").strip()
        if not text:
            continue
        if any(_similar(text, e["content"]) for e in existing):
            continue
        await store.add_foreshadow(work_id, planted_ordinal=ordinal, content=text,
                                   importance=fs.importance)
        stats["foreshadows"] += 1

    for raw in data.resolved_foreshadows:
        text = (raw or "").strip()
        if not text:
            continue
        for e in existing:
            if _similar(text, e["content"]):
                await store.resolve_foreshadow(e["id"], ordinal)
                stats["resolved"] += 1
                break

    return stats


def _similar(a: str, b: str) -> bool:
    """伏笔去重：两条描述是不是同一件事。

    不用固定字符窗口——模型把同一件事写得长一句短一句，窗口匹配就失效了。
    改用 2-gram 重叠率：中文短句里重复出现同样几个词，基本就是同一件事。
    """
    a, b = canon(a), canon(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if len(ga) < 6 or len(gb) < 6:  # 太短的句子重叠率不可信
        return False
    return len(ga & gb) / min(len(ga), len(gb)) >= 0.5


# ==================== 子图查询 ====================

async def subgraph(
    store, work_id: str, *, text: str = "", seeds: Optional[list[str]] = None,
    hops: int = 2, limit: int = 60,
) -> dict[str, Any]:
    """按种子实体的 N 跳邻域取子图。种子可以给名字，也可以给一段文本自动链接。"""
    g = await load_graph(store, work_id)
    entities: list[dict] = g["entities"]

    seed_entities: list[dict] = []
    for name in seeds or []:
        key = canon(name)
        ent = g["by_canon"].get(key)
        if ent is None:
            for e in entities:
                if key and (key == canon(e["name"]) or key in _aliases(e)):
                    ent = e
                    break
        if ent and ent not in seed_entities:
            seed_entities.append(ent)

    if not seed_entities and text:
        seed_entities = _mentions(text, entities)
    if not seed_entities:
        # 没有线索时退到「最近出现的角色」，至少别让 S6 空着
        recent = sorted((e for e in entities if e["kind"] == "character"),
                        key=lambda e: int(e.get("first_ordinal") or 0), reverse=True)
        seed_entities = recent[:8]

    # 邻接表一次建好，BFS 只走内存
    adj: dict[str, list[tuple[str, dict]]] = {}
    for r in g["relations"]:
        adj.setdefault(r["src_id"], []).append((r["dst_id"], r))
        adj.setdefault(r["dst_id"], []).append((r["src_id"], r))

    visited: dict[str, int] = {}
    frontier = [e["id"] for e in seed_entities]
    for eid in frontier:
        visited[eid] = 0
    for depth in range(1, max(1, hops) + 1):
        nxt: list[str] = []
        for eid in frontier:
            for other, rel in adj.get(eid, []):
                if other not in visited:
                    visited[other] = depth
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break

    picked = [e for e in entities if e["id"] in visited]
    picked.sort(key=lambda e: (visited.get(e["id"], 9), KIND_ORDER.get(e["kind"], 9),
                               -int(e.get("first_ordinal") or 0)))
    picked = picked[:limit]
    keep = {e["id"] for e in picked}
    relations = [r for r in g["relations"] if r["src_id"] in keep and r["dst_id"] in keep]

    states: dict[str, dict] = {}
    for e in picked:
        latest = await store.latest_state(e["id"])
        if latest:
            try:
                states[e["id"]] = json.loads(latest["state_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                states[e["id"]] = {}

    related_fs = [f for f in g["foreshadows"] if _mentions(f["content"], picked, limit=1)]
    return {
        "seeds": [e["name"] for e in seed_entities],
        "entities": picked,
        "relations": relations,
        "states": states,
        "foreshadows": related_fs,
        "hops": hops,
    }


KIND_ORDER = {"character": 0, "location": 1, "item": 2, "faction": 3, "rule": 4,
              "event": 5, "foreshadow": 6}


def _aliases(entity: dict) -> set[str]:
    try:
        return {canon(a) for a in json.loads(entity.get("aliases") or "[]")}
    except (json.JSONDecodeError, TypeError):
        return set()


def format_subgraph(g: dict[str, Any], *, budget: int = 2500, current_ordinal: int = 0) -> str:
    """把子图压成进 prompt 的文本。预算吃紧时先砍规则与次要人物。"""
    if not g.get("entities"):
        return ""

    lines: list[str] = []
    used = 0
    state_map: dict[str, dict] = g.get("states") or {}

    def add(text: str) -> bool:
        nonlocal used
        if used + len(text) > budget:
            return False
        lines.append(text)
        used += len(text) + 1
        return True

    grouped: dict[str, list[dict]] = {}
    for e in g["entities"]:
        grouped.setdefault(e["kind"], []).append(e)

    for kind in ("character", "location", "item", "faction", "rule"):
        items = grouped.get(kind) or []
        if not items:
            continue
        block = [f"【{KIND_LABEL.get(kind, kind)}】"]
        for e in items:
            bits = [f"- {e['name']}"]
            extra: list[str] = []
            if e.get("status") and e["status"] != "alive":
                label = STATUS_LABEL.get(e["status"], e["status"])
                at = int(e.get("status_ordinal") or 0)
                # status_ordinal=0 是设定卡写的「一开始就是这样」，写「第 0 节」很怪
                extra.append(f"{label}于第 {at} 节" if at else label)
            st = state_map.get(e["id"]) or {}
            if st.get("note"):
                extra.append(str(st["note"]))
            if st.get("location"):
                extra.append(f"在{st['location']}")
            if st.get("owner"):
                extra.append(f"持有者：{st['owner']}")
            if extra:
                bits.append("（" + "；".join(extra) + "）")
            block.append("".join(bits))
        if not add("\n".join(block)):
            break

    if g.get("relations"):
        block = ["【关系】"]
        for r in g["relations"]:
            block.append(f"- {r.get('src_name')} —{r['rel_type']}→ {r.get('dst_name')}"
                         f"（第 {r.get('since_ordinal') or 0} 节起）")
        add("\n".join(block))

    if g.get("foreshadows"):
        block = ["【未回收伏笔】"]
        for f in g["foreshadows"]:
            tail = ""
            plan = f.get("planned_payoff_ordinal")
            if plan:
                overdue = "（已超期）" if current_ordinal and int(plan) < current_ordinal else ""
                tail = f"（计划第 {plan} 节回收{overdue}）"
            block.append(f"- 第 {f['planted_ordinal']} 节埋：{f['content']}{tail}")
        add("\n".join(block))

    return "\n\n".join(lines)
