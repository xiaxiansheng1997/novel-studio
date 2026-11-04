"""全作品查找替换：改一个名字不该逼人在六十个 textarea 里手点一遍。

写作台有 89 个接口，此前没有一个是 replace 类的。角色改名、统一一个说法、把「她」改成
「他」，都只能在每一节的正文框里逐处手改——而自动保存不留版本，改到第 40 章发现第 3 章
改错了，只能翻回去重看。

三条口径是本模块的全部难点：

1. **替换前先把旧文存进版本表**。版本表存的是「改后」的正文，自动保存不写版本，
   所以不补这一刀，替换前的稿子在任何一个历史列表里都不存在——那不是替换，是毁稿。
   判据与 `store.restore_version` 完全相同（最新版本已经等于当前正文就不重复补），
   所以历史里不会出现两行一模一样的内容。
2. **默认 dry_run**。先看命中什么再决定动不动库，一次替换摊到整本书是几十上百节，
   这种规模的改动不该由一次回车发起。
3. **不做正则**。中文稿里用不上，而一条坏正则在一百万字的库上能跑出不归的耗时，
   界面也很难解释它到底命中了什么。只给「区分大小写」，那是人真正会用的唯一一种模式。
"""
from __future__ import annotations

from typing import Any, Optional

from app.studio import indexer

MAX_FIND_CHARS = 200

# 两句实话，都不自动处理。写在这里而不是拼在界面上：接口与脚本看到的是同一份说明。
WARN_GRAPH = ("图谱里的实体与设定卡的名字不会跟着改：要同步得重跑 "
              "POST /studio/works/{id}/graph/extract，那会真调模型。")
WARN_TERM_EMPTY = "替换后是空串，术语名不能空：术语那一条没改。"


def _fold_map(text: str) -> tuple[str, list[int]]:
    """返回「逐字符 casefold 后的串」与「folded 第 k 个字符来自原文第几个字符」。

    不用 `text.casefold()` 直接找：那个方法带上下文规则（希腊文词尾 σ 会变 ς），
    整串折出来的结果与逐字符折的可能长度不一致，位置一旦对不上，切原文就会切错字。
    """
    parts: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        folded = ch.casefold()
        if not folded:
            continue      # 少数控制字符折成空串：不占索引
        parts.append(folded)
        origin.extend([i] * len(folded))
    return "".join(parts), origin


def find_all(text: str, find: str, *, case_sensitive: bool = True) -> list[tuple[int, int]]:
    """`find` 在 `text` 里的全部命中区间（左闭右开）。空串不算命中。

    大小写不敏感时命中区间一律按**原文字符**边界取整：一个字符折成好几个的
    （ß、ﬁ 这类）只留第一次，落在同一个原文字符里的第二次算重叠，丢掉——
    宁可少报一处，也不能切出半个字。
    """
    if not text or not find:
        return []
    out: list[tuple[int, int]] = []
    if case_sensitive:
        start = text.find(find)
        while start >= 0:
            out.append((start, start + len(find)))
            start = text.find(find, start + len(find))
        return out
    folded, origin = _fold_map(text)
    needle = "".join(ch.casefold() for ch in find)
    if not needle:
        return []
    start = folded.find(needle)
    while start >= 0:
        lo = origin[start]
        hi = origin[min(start + len(needle) - 1, len(origin) - 1)] + 1
        if out and lo < out[-1][1]:
            lo = -1        # 与前一次重叠：同一段原文不重复算
        if lo >= 0:
            out.append((lo, hi))
        start = folded.find(needle, start + len(needle))
    return out


def apply_replace(
    text: str, find: str, replace: str = "", *, case_sensitive: bool = True,
) -> tuple[str, int]:
    """替换并返回 (新正文, 命中处数)。切片一律用原文，不整串降格成小写。"""
    hits = find_all(text, find, case_sensitive=case_sensitive)
    if not hits:
        return text, 0
    parts: list[str] = []
    cursor = 0
    for lo, hi in hits:
        parts.append(text[cursor:lo])
        parts.append(replace or "")
        cursor = hi
    parts.append(text[cursor:])
    return "".join(parts), len(hits)


def scan_node(
    node: dict[str, Any], find: str, replace: str, *, case_sensitive: bool,
) -> Optional[dict[str, Any]]:
    """看一节里有几处命中。没命中返回 None（调用方按「这节不动」处理）。"""
    content = node.get("content") or ""
    new_text, hits = apply_replace(content, find, replace, case_sensitive=case_sensitive)
    if not hits:
        return None
    return {
        "node_id": node["id"],
        "title": node.get("title") or "",
        "ordinal": int(node.get("ordinal") or 0),
        "count": hits,
        "chars_before": len(content),
        "chars_after": len(new_text),
    }


async def replace_in_work(
    store,
    *,
    work_id: str,
    find: str,
    replace: str = "",
    scope: str = "work",
    node_id: str = "",
    case_sensitive: bool = True,
    dry_run: bool = True,
    update_terms: bool = True,
) -> dict[str, Any]:
    """在一本书（或其中一节）里做查找替换。`dry_run=True` 时一个字都不写。

    取节走 `list_readable`：与整本检查、导出、索引重建是同一条路径，所以「能被替换到的
    范围」与「能被读到的范围」天然一致，不会出现导出里有、替换摸不着的节。
    """
    targets: list[dict[str, Any]] = []
    if scope == "node":
        node = await store.get_node(node_id)
        if node and node.get("work_id") != work_id:
            node = None      # 借别的作品的节 id 来打这一本：当不存在
        targets = [node] if node else []
    else:
        for item in await store.list_readable(work_id):
            node = await store.get_node(item["id"])
            if node:
                targets.append(node)

    matches: list[dict[str, Any]] = []
    for node in targets:
        hit = scan_node(node, find, replace, case_sensitive=case_sensitive)
        if hit:
            matches.append(hit)

    terms = await store.list_terms(work_id)
    same_terms = [t for t in terms if (t.get("term") or "") == find]
    term_hits = len(same_terms)

    if dry_run:
        return {
            "dry_run": True,
            "scope": scope,
            "find": find,
            "replace": replace,
            "case_sensitive": case_sensitive,
            "matches": matches,
            "total_hits": sum(m["count"] for m in matches),
            "total_nodes": len(matches),
            "scanned_nodes": len(targets),
            "term_hits": term_hits,
            "warnings": _warnings(len(matches), term_hits, replace, update_terms),
        }

    warnings = _warnings(len(matches), term_hits, replace, update_terms)
    replaced = 0
    hits_done = 0
    for m in matches:
        old = await store.get_node(m["node_id"])
        if not old:
            continue       # 预览与落库之间那一节被删了：跳过，不凭空造版本
        old_content = old.get("content") or ""
        # 在「刚读到的那一份正文」上重算替换，而不是用扫描阶段算好的新文：
        # 两次读之间后台队列可能已经改过这一节，拿旧稿算出的新文写回去会吞掉那段改动
        new_content, hits = apply_replace(
            old_content, find, replace, case_sensitive=case_sensitive)
        if not hits:
            continue       # 同样的原因：这一节已经被改过了，不该再动它
        # 1) 先保旧文（判据同 restore_version：已经进过版本表就不重复补）
        await store.preserve_current_version(m["node_id"], old_content)
        # 2) 再写新文。快照来源单独一档，`bulk_replace` 不该把「今天写的字数」刷高
        await store.update_node(m["node_id"], content=new_content,
                                snapshot=True, snapshot_source="bulk_replace")
        replaced += 1
        hits_done += hits

    terms_updated: list[dict[str, Any]] = []
    if update_terms and term_hits and replace:
        for t in same_terms:
            try:
                await store.update_term(t["id"], term=replace)
            except ValueError as exc:
                # 撞 UNIQUE(work_id, term)：那是「已经有同名术语了」，不是系统故障。
                # 记一句就继续，不能因为一个术语把整批替换毁掉
                warnings.append(f"术语「{t.get('term') or ''}」没改成：{exc}")
                continue
            terms_updated.append({"term_id": t["id"], "from": find, "to": replace})

    # 索引交给增量重建：没改的节按 content_hash 自己跳过，术语也会先灌进分词词典。
    # 一个字都没改就不跑：那一趟扫完全书是白烧的活
    index = await _reindex(store, work_id) if replaced else \
        {"skipped": True, "reason": "没改成一个字，索引不用重跑"}
    return {
        "dry_run": False,
        "scope": scope,
        "find": find,
        "replace": replace,
        "case_sensitive": case_sensitive,
        "replaced_nodes": replaced,
        "replaced_hits": hits_done,
        "terms_updated": terms_updated,
        "index": index,
        # 正文一改，节摘要的 ref_hash 就对不上、卷/全书摘要已被置空：都要重算
        "summaries_stale": replaced,
        "warnings": warnings,
    }


def _warnings(nodes: int, term_hits: int, replace: str, update_terms: bool) -> list[str]:
    """改动之后仍然要人自己做的那些事。不做的事不假装做了。"""
    if not nodes:
        return []
    out = [
        WARN_GRAPH,
        f"{nodes} 节摘要与卷/全书摘要已判过期：右栏「记忆」会显示待重算，重算要花钱。",
    ]
    if term_hits and update_terms and not replace:
        out.append(WARN_TERM_EMPTY)
    return out


async def _reindex(store, work_id: str) -> dict[str, Any]:
    """索引重建失败不推翻替换：稿子已经改对了，检索晚一点补上就行。"""
    try:
        return await indexer.reindex_work(store, work_id)
    except Exception as exc:  # noqa: BLE001 - 分词/建块任何一步炸都不该让替换回滚
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
