"""一致性检查：小说 5 条硬规则 + 文档 4 条硬规则（软规则见 `soft_rules.py`）。

为什么不调 LLM：

- 这 9 条都是结构化比对，本地算一遍是毫秒级。每次保存都跑得起，
  而一旦要调模型就必然被「省一次调用」的念头挤到「定稿才跑」，
  问题就会攒到最后一起爆。
- 模型判「设定冲突」误报率高，报出来的东西人不敢信，久了就全忽略。
  所以硬规则只保留高精度的，精度不够的降级成 info 交给 soft_rules。

所有规则都只读不写正文，命中项落到同一张 `issues` 表，前端一套告警条统一渲染。
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Optional

from app.studio import soft_rules

NOVEL_RULES = ("dead_character", "item_conflict", "time_regression",
               "location_destroyed", "foreshadow_overdue")
DOC_RULES = ("fact_stale", "citation_missing", "term_inconsistent", "structure_incomplete")
# 软规则：精度不够「必然错」，一律 info，不参与打分（见 soft_rules 模块头的分工表）
SOFT_NOVEL_RULES = soft_rules.NOVEL_SOFT
SOFT_DOC_RULES = soft_rules.DOC_SOFT
ALL_NOVEL = NOVEL_RULES + SOFT_NOVEL_RULES
ALL_DOC = DOC_RULES + SOFT_DOC_RULES

YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*年?")
BIGNUM_RE = re.compile(r"\d{3,}")
CITE_RE = re.compile(
    r"(据[^，。]{0,12}(?:报道|统计|记载|介绍)|来源[:：]|参考(?:资料|文献)|引自|见《[^》]{1,20}》"
    r"|《[^》]{2,20}》|新华社|人民日报|数据显示|官方通报)"
)
MEMORY_RE = re.compile(r"(回忆|想起|记起|生前|当年|梦里|梦中|尸体|遗体|墓碑|遗物|遗言|曾说过|记得|往事)")
# 段落切成「句」用来定位证据；保留标点，方便原样回填到正文里搜
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")


# ==================== 工具 ====================

def _paragraphs(content: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    idx = 0
    for block in re.split(r"\n\s*\n|\n", content or ""):
        text = block.strip()
        if not text:
            continue
        idx += 1
        out.append((idx, text))
    return out


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text) if s.strip()]


def _snippet(text: str, start: int, end: int, pad: int = 24) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    return text[lo:hi].replace("\n", " ").strip()


def _aliases(entity: dict) -> list[str]:
    try:
        return [a for a in json.loads(entity.get("aliases") or "[]") if a]
    except (json.JSONDecodeError, TypeError):
        return []


def _sentence_at(text: str, pos: int) -> str:
    """取某一位置所在的句子。判「回忆/墓碑」这类上下文必须按句看，
    按字符窗口会在长段里把相邻的回忆句与在场句混在一起。"""
    marks = "。！？!?\n"
    start = 0
    for i in range(pos - 1, -1, -1):
        if text[i] in marks:
            start = i + 1
            break
    end = len(text)
    for i in range(pos, len(text)):
        if text[i] in marks:
            end = i + 1
            break
    return text[start:end]


def _live_mention(content: str, names: list[str]) -> Optional[tuple[str, int, int]]:
    """找出「当作在场角色」的提及。

    回忆、墓碑、遗物这类句子是合法的提及，不能报「死人复活」。
    判据按整句看（而非固定字符窗口），宁可漏掉几句，也不要每次都误报逼人关掉告警。
    """
    for name in names:
        if not name or len(name) < 2:
            continue
        for m in re.finditer(re.escape(name), content):
            if MEMORY_RE.search(_sentence_at(content, m.start())):
                continue
            return name, m.start(), m.end()
    return None


def _parse_day(raw: str) -> Optional[int]:
    """把 story_time 解析成可比的整数。只认绝对日期与「第N天」，
    相对表述（三年后、次日）直接放弃——宁可漏报，也不要在时间轴上瞎猜。"""
    s = (raw or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    m = re.match(r"^第?\s*(\d{1,5})\s*天", s)
    if m:
        return int(m.group(1))
    return None


# ==================== 小说规则 ====================

async def _novel_rules(store, work, node: dict, content: str, ordinal: int) -> list[dict]:
    issues: list[dict] = []
    entities = await store.list_entities(work["id"])
    if not entities:
        return issues
    relation_rows = await store.list_relations(work["id"])

    # 1. 已死亡角色再次出场
    for e in entities:
        if e.get("status") != "dead":
            continue
        if int(e.get("status_ordinal") or 0) >= ordinal:
            continue  # 本节才死的，不算复活
        hit = _live_mention(content, [e["name"]] + _aliases(e))
        if not hit:
            continue
        _, start, end = hit
        at = int(e.get("status_ordinal") or 0)
        when = f"已在第 {at} 节死亡" if at else "开篇前就已经死亡"
        issues.append({
            "rule": "dead_character", "severity": "error",
            "message": f"{e['name']} {when}，本节仍作为在场角色出现",
            "evidence": _snippet(content, start, end),
            "location": str(ordinal),
        })

    # 2. 物品归属冲突：同一物品有多个未失效的持有者，且两人都在本节出现
    held: dict[str, list[dict]] = {}
    for r in relation_rows:
        if r.get("rel_type") != "持有" or r.get("until_ordinal") is not None:
            continue
        held.setdefault(r["dst_id"], []).append(r)
    for item_id, rows in held.items():
        if len({r["src_id"] for r in rows}) < 2:
            continue
        item = next((e for e in entities if e["id"] == item_id), None)
        if not item:
            continue
        present = [r for r in rows if r["src_name"] in content]
        if len({r["src_id"] for r in present}) >= 2:
            names = "、".join(sorted({r["src_name"] for r in present}))
            issues.append({
                "rule": "item_conflict", "severity": "error",
                "message": f"{item['name']} 同时被 {names} 持有，且都出现在本节",
                "evidence": next((s for s in _sentences(content) if item["name"] in s), "")[:160],
                "location": str(ordinal),
            })

    # 3. 时间倒退：本节 story_time 早于上一节
    events = await store.list_events(work["id"], limit=800)
    by_ordinal: dict[int, str] = {}
    for ev in events:
        if ev.get("story_time"):
            by_ordinal[int(ev["ordinal"])] = ev["story_time"]
    cur_t = _parse_day(by_ordinal.get(ordinal, ""))
    prev = [(o, _parse_day(t)) for o, t in by_ordinal.items() if o < ordinal]
    prev = [(o, k) for o, k in prev if k is not None]
    if cur_t is not None and prev:
        last_o, last_t = max(prev, key=lambda x: x[0])
        if cur_t < last_t:
            issues.append({
                "rule": "time_regression", "severity": "error",
                "message": f"本节故事时间（{by_ordinal[ordinal]}）早于第 {last_o} 节（{by_ordinal[last_o]}）",
                # 故事时间是抽取出来的，不在正文里；留空证据避免前端「定位」点了没反应
                "evidence": "",
                "location": str(ordinal),
            })

    # 4. 已毁/已失地点仍作为场景
    for e in entities:
        if e.get("kind") != "location" or e.get("status") not in ("destroyed", "missing"):
            continue
        if int(e.get("status_ordinal") or 0) >= ordinal:
            continue
        if e["name"] not in content:
            continue
        scene = next((ev for ev in events
                      if int(ev["ordinal"]) == ordinal and ev.get("location_id") == e["id"]), None)
        names = [e["name"]] + _aliases(e)
        hit = next((n for n in names if n and n in content), e["name"])
        degraded = "被毁" if e["status"] == "destroyed" else "失落"
        at = int(e.get("status_ordinal") or 0)
        when = f"已于第 {at} 节{degraded}" if at else f"开篇前就已{degraded}"
        issues.append({
            "rule": "location_destroyed", "severity": "error" if scene else "warn",
            "message": f"{e['name']} {when}，本节仍作场景使用",
            "evidence": next((s for s in _sentences(content) if hit in s), "")[:160],
            "location": str(ordinal),
        })

    return issues


async def _foreshadow_overdue(store, work, ordinal: int) -> list[dict]:
    out: list[dict] = []
    for f in await store.list_foreshadows(work["id"], status="open"):
        plan = f.get("planned_payoff_ordinal")
        if plan and int(plan) < ordinal:
            out.append({
                "rule": "foreshadow_overdue", "severity": "warn",
                "message": (f"第 {f['planted_ordinal']} 节埋的伏笔已超期"
                            f"（计划第 {plan} 节回收）：{f['content'][:60]}"),
                "evidence": "",  # 伏笔原文来自别的章节，在本节点里定位不到
                "location": str(f["planted_ordinal"]),
            })
    return out


# ==================== 文档规则 ====================

async def _fact_stale(store, work) -> list[dict]:
    """引用的事实过了有效期。时政类最要命——两年前的"最新"比不写还坏。"""
    out: list[dict] = []
    today = date.today().isoformat()
    for f in await store.list_facts(work["id"], limit=500):
        until = (f.get("valid_until") or "").strip()
        if until and until < today:
            out.append({
                "rule": "fact_stale", "severity": "error",
                "message": f"事实已过期（有效期至 {until}）：{f['statement'][:60]}",
                "evidence": "",  # 事实卡的内容不一定原样出现在正文里
                "location": "",
            })
    return out


async def _doc_rules(store, work, content: str, resolved: dict) -> list[dict]:
    issues: list[dict] = []
    rules = set(resolved.get("checks") or DOC_RULES)
    requires_sources = bool(resolved.get("requires_sources"))

    # 事实过期是全局属性（按今天算，与哪一节无关），放到 run_checks 的整篇分支跑，
    # 否则每节都会把同一条过期事实报一遍。

    # 2. 有数字/年份但无来源
    if "citation_missing" in rules:
        for idx, para in _paragraphs(content):
            if CITE_RE.search(para):
                continue
            hits = YEAR_RE.findall(para) + BIGNUM_RE.findall(para)
            if not hits:
                continue
            issues.append({
                "rule": "citation_missing", "severity": "warn" if not requires_sources else "error",
                "message": f"第 {idx} 段含具体数字/年份（{hits[0]}）但未标注来源",
                "evidence": para[:160],
                "location": str(idx),
            })

    # 3. 术语不一致：命中禁用词，或用词不是首选写法
    if "term_inconsistent" in rules:
        for t in await store.list_terms(work["id"]):
            banned: list[str] = []
            try:
                banned = json.loads(t.get("forbidden") or "[]")
            except (json.JSONDecodeError, TypeError):
                pass
            for bad in banned:
                if bad and bad in content:
                    issues.append({
                        "rule": "term_inconsistent", "severity": "warn",
                        "message": f"「{bad}」是禁用写法，应改为「{t.get('preferred') or t['term']}」",
                        "evidence": next((s for s in _sentences(content) if bad in s), "")[:160],
                        "location": "",
                    })
            preferred = (t.get("preferred") or "").strip()
            if preferred and preferred != t["term"] and t["term"] in content and preferred not in content:
                issues.append({
                    "rule": "term_inconsistent", "severity": "info",
                    "message": f"出现「{t['term']}」，本篇统一用「{preferred}」",
                    "evidence": next((s for s in _sentences(content) if t["term"] in s), "")[:160],
                    "location": "",
                })

    return issues


def template_outline(template: Optional[dict]) -> list[dict]:
    """模板骨架可能来自内存（outline）或数据库行（outline_json），两处都得认。"""
    if not template:
        return []
    raw = template.get("outline")
    if isinstance(raw, list):
        return raw
    try:
        got = json.loads(template.get("outline_json") or "[]")
        return got if isinstance(got, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def _structure(store, work, resolved: dict, template: Optional[dict]) -> list[dict]:
    """模板要求的节是否齐全、字数是否严重偏离。只在单篇（无容器层）上判。"""
    if resolved.get("container_kind"):
        return []
    issues: list[dict] = []
    want = [o.get("heading") for o in template_outline(template) if o.get("heading")]
    nodes = await store.list_readable(work["id"])
    if want and len(nodes) < max(2, len(want) - 2):
        issues.append({
            "rule": "structure_incomplete", "severity": "warn",
            "message": f"模板要求约 {len(want)} 节，当前只有 {len(nodes)} 节",
            "evidence": "、".join(want[:8]),
            "location": "",
        })
    target = int(resolved.get("target_words") or 0)
    if target and nodes:
        total = sum(int(n.get("word_count") or 0) for n in nodes)
        if total and (total < target * 0.5 or total > target * 2):
            issues.append({
                "rule": "structure_incomplete", "severity": "warn",
                "message": f"全书 {total} 字，目标 {target} 字，偏离超过一倍",
                "evidence": "", "location": "",
            })
    return issues


# ==================== 入口 ====================

async def run_checks(
    store, work: dict[str, Any], node: Optional[dict[str, Any]] = None, *,
    resolved: Optional[dict[str, Any]] = None, template: Optional[dict] = None,
) -> list[dict[str, Any]]:
    """跑一遍检查并写 issues。

    `node` 为空时做全书检查（伏笔超期、过期事实、结构完整性、跨节年龄对账）；
    给 node 时只查这一节。重跑前清掉本节旧告警——改稿后结论就该重判。

    硬规则与软规则的区别只有一条：硬规则「必然错」，软规则「八成有问题」，
    所以后者一律 info、不进扣分链路，而且必须在 `checks` 里显式开着才跑。
    """
    from app.studio import content_types

    resolved = resolved or content_types.resolve(
        work.get("kind") or "article", work.get("scale") or "", work.get("subtype") or "")
    is_novel = (work.get("kind") or "article") == "novel"
    rules = set(resolved.get("checks") or [])
    found: list[dict] = []

    if node is not None:
        content = (node.get("content") or "").strip()
        ordinal = int(node.get("ordinal") or 0)
        await store.clear_issues(work["id"], node["id"])
        if content and len(content) >= 80:
            if is_novel:
                found += await _novel_rules(store, work, node, content, ordinal)
            else:
                found += await _doc_rules(store, work, content, resolved)
            found += await soft_rules.check_node(store, work, node, content, ordinal, rules)
        for item in found:
            item["node_id"] = node["id"]
    else:
        last = max((int(n.get("ordinal") or 0) for n in await store.list_readable(work["id"])),
                   default=0)
        if is_novel and last:
            found += await _foreshadow_overdue(store, work, last + 1)
        if not is_novel:
            found += await _structure(store, work, resolved, template)
            if "fact_stale" in (rules or set(DOC_RULES)):
                found += await _fact_stale(store, work)
        # 跨节对账要把每节正文读一遍，只在「整本检查」这条路径上跑
        found += await soft_rules.check_whole_book(store, work, rules)
        for item in found:
            # 整本软规则已经把告警挂到「出问题那一节」上，不能被这里抹成 None，
            # 否则前端既看不到也点不动「定位」。
            item.setdefault("node_id", None)

    created: list[dict] = []
    for item in found:
        if await store.issue_exists(work["id"], item.get("node_id"), item["rule"],
                                    item["message"]):
            continue
        created.append(await store.add_issue(
            work["id"], rule=item["rule"], message=item["message"],
            node_id=item.get("node_id"), severity=item.get("severity", "warn"),
            evidence=item.get("evidence", ""), location=item.get("location", ""),
        ))
    return created


async def check_summary(store, work_id: str) -> dict[str, Any]:
    """面板用的一句话统计。"""
    rows = await store.list_issues(work_id, only_open=True, limit=500)
    by_sev: dict[str, int] = {}
    for r in rows:
        by_sev[r.get("severity") or "warn"] = by_sev.get(r.get("severity") or "warn", 0) + 1
    return {
        "open": len(rows),
        "errors": by_sev.get("error", 0),
        "warnings": by_sev.get("warn", 0),
        "infos": by_sev.get("info", 0),
        "by_rule": {r: sum(1 for x in rows if x["rule"] == r) for r in {x["rule"] for x in rows}},
    }
