"""手艺拆解：把一本已成稿的书拆成「可操作的结构事实」。

三级流水线：逐章（场景单元/冲突/信息差/钩子/职能）→ 卷（弧线/升级/转折/伏笔对）
→ 全书（幕结构/主线/节奏/可借鉴点）。一次拆解服务两个下游：产物可以拷进
别的书的素材桶（S_src 检索得到），全书的幕结构可以抽成模板——都不是仿写，
是把手艺变成下次写作时看得见的参考。

与 summarizer 的分工要说清：摘要回答「前面发生了什么」（喂自己书的续写），
这里回答「它是怎么发生的」（喂别的书的新写作）。所以两边提示词完全不同，
但增量指纹、JSON 宽松解析、force 跳过这些机制全部照抄现成范式。

提示词的收敛口径：只问结构事实，禁止「赏析」「文笔评价」——模型被问
"这一章好在哪里" 会输出营销号，被问 "这一章的冲突是什么、读者比角色多知道
什么" 才会输出拆得动的零件。
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from app.studio import cost, jsonutil, sampling
# 口径必须与 nodes.content_hash 完全一致（同 summarizer：sha1 前 16 位、不 strip），
# 自己另算一个会让每章都被判过期、每次都重烧一遍调用。
from app.studio.store import _hash

# 单章输入上限：反代与直连到这个量级都稳。超长截前段——拆解看的是
# 「怎么开局、埋了什么」，前 1.2 万字足够看清一章的手艺，尾部剧情梗概反而便宜。
CHAPTER_MAX_CHARS = 12000
# 太短的章不拆：两三百字里模型只会编造结构。
MIN_CHAPTER_CHARS = 200
# 卷/全书级输入是下层卡片的拼接，同样设上限防超长（60 章 × 数百字通常用不到）
AGGREGATE_MAX_CHARS = 20000

CHAPTER_SYSTEM = (
    "你是中文小说的结构分析员，像拆发动机的技师，不做书评人。"
    "只输出合法 JSON，不输出解释、前言或 Markdown 代码块标记。"
)

CHAPTER_USER = """拆下面这一章，只回答结构事实，禁止评价文笔：

{{"scenes":[{{"unit":"这个场景单元在干什么（一句话）","conflict":"双方争什么","info_gap":"读者知道而角色不知道（或反之）的是什么"}}],"hook":"章末钩子：类型（悬念/反转/新威胁/情感钩）+ 一句话","role":"本章职能：铺垫/升级/转折/过渡/收束"}}

要求：scenes 按出场顺序 1-4 个；conflict 或 info_gap 确实没有就写「无」；
全部字段用中文，每项不超过 40 字。

<section title="{title}">
{content}
</section>"""

VOLUME_SYSTEM = (
    "你是中文小说的结构分析员，负责把一章章的结构卡片合并成卷级结构。"
    "只输出合法 JSON，不输出解释、前言或 Markdown 代码块标记。"
)

VOLUME_USER = """下面是「{label}」各章的结构卡片（按顺序）。合并出这一卷的结构，只写结构事实：

{{"arc":"这一卷的主线弧线（一句话）","escalation":"冲突在本卷内如何升级（起点→终点）","turning_points":[{{"at":"第N章","what":"发生了什么转折"}}],"foreshadows":[{{"plant":"第N章埋下","payoff":"第M章收回，或「未收」","what":"伏笔内容"}}]}}

要求：turning_points 与 foreshadows 各 0-5 条，宁缺毋滥；章节定位用卡片前面的编号；
全部字段用中文，每项不超过 40 字。

{parts}"""

BOOK_SYSTEM = (
    "你是中文小说的结构分析员，负责把各卷结构合并成全书架构分析。"
    "只输出合法 JSON，不输出解释、前言或 Markdown 代码块标记。"
)

BOOK_USER = """下面是《{title}》的 premise 与各卷结构卡片。输出这本书的整体架构，只写可操作的结构事实：

{{"structure_type":"结构类型一句话（如：三幕式/循环升级/双线并进）","acts":[{{"name":"幕名","span":"覆盖哪些卷或章","function":"这一幕在结构上干什么"}}],"main_lines":["主线冲突，1-3 条"],"rhythm":"节奏模式：高压段与舒缓段是怎么交替的","takeaways":["值得带进下一本新书的结构做法，2-5 条，每条一句可执行的话"]}}

要求：acts 2-5 个按顺序；takeaways 别写「人物要立体」这种空话，写「开局第三章就让主角主动犯错付出代价」这种能照着做的话。

premise：{premise}

{parts}"""


def _card_lines(rows: list[dict[str, Any]], *, label_key: str = "title") -> str:
    """把下层卡片拼成带编号的输入段。"""
    parts = []
    for i, r in enumerate(rows, 1):
        body = (r.get("text") or "").strip()
        title = (r.get(label_key) or "").strip() or "未命名"
        parts.append(f"【{i}】{title}\n{body}")
    merged = "\n\n".join(parts)
    return merged[:AGGREGATE_MAX_CHARS]


def render_chapter(data: dict[str, Any]) -> str:
    """章卡片 → Markdown。素材桶里躺的就是这份，要一眼能读。"""
    lines = []
    scenes = jsonutil.as_list(data.get("scenes"))
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            continue
        unit = jsonutil.as_str(sc.get("unit"), 80)
        conflict = jsonutil.as_str(sc.get("conflict"), 60)
        gap = jsonutil.as_str(sc.get("info_gap"), 60)
        lines.append(f"{i}. {unit}")
        if conflict and conflict != "无":
            lines.append(f"   - 冲突：{conflict}")
        if gap and gap != "无":
            lines.append(f"   - 信息差：{gap}")
    hook = jsonutil.as_str(data.get("hook"), 80)
    if hook:
        lines.append(f"钩子：{hook}")
    role = jsonutil.as_str(data.get("role"), 40)
    if role:
        lines.append(f"职能：{role}")
    return "\n".join(lines)


def render_volume(data: dict[str, Any]) -> str:
    lines = []
    arc = jsonutil.as_str(data.get("arc"), 100)
    if arc:
        lines.append(f"弧线：{arc}")
    esc = jsonutil.as_str(data.get("escalation"), 100)
    if esc:
        lines.append(f"升级：{esc}")
    tps = jsonutil.as_list(data.get("turning_points"))
    if tps:
        lines.append("转折：")
        for t in tps[:6]:
            if isinstance(t, dict):
                lines.append(f"- {jsonutil.as_str(t.get('at'), 20)}：{jsonutil.as_str(t.get('what'), 60)}")
    fs = jsonutil.as_list(data.get("foreshadows"))
    if fs:
        lines.append("伏笔：")
        for f in fs[:8]:
            if isinstance(f, dict):
                lines.append(
                    f"- {jsonutil.as_str(f.get('what'), 40)}（{jsonutil.as_str(f.get('plant'), 16)} → {jsonutil.as_str(f.get('payoff'), 16)}）")
    return "\n".join(lines)


def render_book(data: dict[str, Any]) -> str:
    lines = []
    st = jsonutil.as_str(data.get("structure_type"), 100)
    if st:
        lines.append(f"结构类型：{st}")
    acts = jsonutil.as_list(data.get("acts"))
    if acts:
        lines.append("幕结构：")
        for a in acts[:6]:
            if isinstance(a, dict):
                lines.append(
                    f"- {jsonutil.as_str(a.get('name'), 30)}（{jsonutil.as_str(a.get('span'), 30)}）：{jsonutil.as_str(a.get('function'), 60)}")
    mains = jsonutil.as_list(data.get("main_lines"))
    if mains:
        lines.append("主线：" + "；".join(jsonutil.as_str(m, 60) for m in mains[:3]))
    rhythm = jsonutil.as_str(data.get("rhythm"), 120)
    if rhythm:
        lines.append(f"节奏：{rhythm}")
    takes = jsonutil.as_list(data.get("takeaways"))
    if takes:
        lines.append("可借鉴：")
        for t in takes[:6]:
            lines.append(f"- {jsonutil.as_str(t, 80)}")
    return "\n".join(lines)


RENDERERS = {"chapter": render_chapter, "volume": render_volume, "book": render_book}


def _sane(data: Any) -> bool:
    """模型返回的 JSON 至少要有一个认识的键，否则视为失败（防空对象把旧卡片覆盖掉）。"""
    return isinstance(data, dict) and any(data.values())


async def analyze_chapter(gateway, store, node: dict[str, Any], *,
                          model: str, force: bool = False) -> dict[str, Any]:
    """拆一章并落卡片。返回 {"status": ok|skipped|short, "chars": n}。"""
    content_raw = node.get("content") or ""
    content = content_raw.strip()
    if len(content) < MIN_CHAPTER_CHARS:
        return {"status": "short", "chars": len(content)}
    digest = _hash(content_raw)
    if not force:
        exist = await store.get_craft(node["work_id"], "chapter", node["id"])
        if exist and (exist.get("ref_hash") or "") == digest:
            return {"status": "skipped", "chars": 0}

    note = ""
    snippet = content
    if len(snippet) > CHAPTER_MAX_CHARS:
        note = f"原文 {len(snippet)} 字，输入已截前 {CHAPTER_MAX_CHARS} 字"
        snippet = snippet[:CHAPTER_MAX_CHARS]
    res = await gateway.complete(
        model=model, system=CHAPTER_SYSTEM,
        prompt=CHAPTER_USER.format(title=node.get("title") or "", content=snippet),
        mode="quick", **sampling.for_task("craft"))
    data = jsonutil.parse_json_loose(res.text)
    if not _sane(data):
        raise ValueError("章拆解返回不是有效 JSON")
    text = render_chapter(data)
    if not text:
        raise ValueError("章拆解结果为空")
    await store.upsert_craft(
        node["work_id"], level="chapter", ref_node_id=node["id"],
        title=node.get("title") or "", content_json=json.dumps(data, ensure_ascii=False),
        text=text, ref_hash=digest, model=model,
        provider=gateway.resolve(model)[0], note=note,
        ref_ordinal=int(node.get("ordinal") or 0))
    return {"status": "ok", "chars": len(text)}


async def analyze_volume(gateway, store, work: dict[str, Any], volume: dict[str, Any], *,
                         model: str, force: bool = False) -> dict[str, Any]:
    """拆一卷：输入是本卷各章的卡片，不是正文——章级没跑完就该排队等着。"""
    tree = await store.list_tree(work["id"])
    # 短章（< MIN_CHAPTER_CHARS）入队时就被过滤了，永远不会产卡片；拿它算
    # 「未拆解」会让卷级永远等不到、白跑两次重试。统计口径必须与路由一致。
    readable_ids = {n["id"] for n in await store.list_readable(work["id"])
                    if int(n.get("chars") or 0) >= MIN_CHAPTER_CHARS}
    child_ids = {n["id"] for n in tree
                 if n.get("parent_id") == volume["id"] and n["id"] in readable_ids}
    if not child_ids:
        return {"status": "short", "chars": 0}
    cards = [c for c in await store.list_craft(work["id"], "chapter")
             if c.get("ref_node_id") in child_ids]
    missing = len(child_ids) - len(cards)
    if missing > 0:
        raise ValueError(f"本卷还有 {missing} 章未拆解：等章级任务跑完再重试")
    merged_src = _card_lines(cards)
    digest = _hash(merged_src)
    if not force:
        exist = await store.get_craft(work["id"], "volume", volume["id"])
        if exist and (exist.get("ref_hash") or "") == digest:
            return {"status": "skipped", "chars": 0}
    res = await gateway.complete(
        model=model, system=VOLUME_SYSTEM,
        prompt=VOLUME_USER.format(label=volume.get("title") or "本卷", parts=merged_src),
        mode="quick", **sampling.for_task("craft"))
    data = jsonutil.parse_json_loose(res.text)
    if not _sane(data):
        raise ValueError("卷拆解返回不是有效 JSON")
    text = render_volume(data)
    if not text:
        raise ValueError("卷拆解结果为空")
    await store.upsert_craft(
        work["id"], level="volume", ref_node_id=volume["id"],
        title=volume.get("title") or "", content_json=json.dumps(data, ensure_ascii=False),
        text=text, ref_hash=digest, model=model,
        provider=gateway.resolve(model)[0],
        ref_ordinal=int(volume.get("ordinal") or 0))
    return {"status": "ok", "chars": len(text)}


async def analyze_book(gateway, store, work: dict[str, Any], *,
                       model: str, force: bool = False) -> dict[str, Any]:
    """拆全书：优先吃卷卡片；没有卷级（单层树/系列文档）就直接吃章卡片。"""
    volumes = await store.list_craft(work["id"], "volume")
    if volumes:
        parts_src = _card_lines(volumes)
    else:
        chapters = await store.list_craft(work["id"], "chapter")
        if not chapters:
            raise ValueError("还没有任何章卡片：先跑章级再跑全书")
        parts_src = _card_lines(chapters)
    digest = _hash(parts_src)
    if not force:
        exist = await store.get_craft(work["id"], "book", "")
        if exist and (exist.get("ref_hash") or "") == digest:
            return {"status": "skipped", "chars": 0}
    res = await gateway.complete(
        model=model, system=BOOK_SYSTEM,
        prompt=BOOK_USER.format(title=work.get("title") or "本书",
                                premise=work.get("premise") or "（未填）",
                                parts=parts_src),
        mode="quick", **sampling.for_task("craft"))
    data = jsonutil.parse_json_loose(res.text)
    if not _sane(data):
        raise ValueError("全书拆解返回不是有效 JSON")
    text = render_book(data)
    if not text:
        raise ValueError("全书拆解结果为空")
    await store.upsert_craft(
        work["id"], level="book", ref_node_id="",
        title=work.get("title") or "", content_json=json.dumps(data, ensure_ascii=False),
        text=text, ref_hash=digest, model=model, provider=gateway.resolve(model)[0])
    return {"status": "ok", "chars": len(text)}


async def run_craft(ctx: Any) -> dict[str, Any]:
    """队列执行器入口：payload = {"level": chapter|volume|book, "force": bool}。"""
    payload = ctx.payload or {}
    level = str(payload.get("level") or "")
    force = bool(payload.get("force"))
    model = ctx.model
    store, work, node = ctx.store, ctx.work, ctx.node
    if level == "chapter":
        if not node:
            raise ValueError("chapter 拆解需要 node_id")
        coro = analyze_chapter(ctx.gateway, store, node, model=model, force=force)
    elif level == "volume":
        if not node:
            raise ValueError("volume 拆解需要 node_id")
        coro = analyze_volume(ctx.gateway, store, work, node, model=model, force=force)
    elif level == "book":
        coro = analyze_book(ctx.gateway, store, work, model=model, force=force)
    else:
        raise ValueError(f"未知拆解层级: {level}")
    result = await coro
    if result.get("status") == "skipped":
        logger.info(f"[craft] {level} 指纹未变，跳过 node={node['id'] if node else ''}")
    return result


def estimate_craft(input_chars_list: list[int], agg_calls: int, *,
                   registry: Any, model: str, spent_before: float = 0.0,
                   budget: float = 0.0) -> dict[str, Any]:
    """拆解的成本预估：章级输入是真实正文长度，卷/全书按卡片拼接的保守上限。

    与 cost.estimate_pipeline 同构（priced/est_cost/over_budget…），前端那套
    提示组件不用改口径；依据不同：这里量的是输入侧真数，不拿历史 run 中位数
    ——拆解的输出（几百字卡片）远小于正文生成，拿写正文的底子来估会高估一倍。
    """
    prompt_total = sum(min(c, CHAPTER_MAX_CHARS) for c in input_chars_list)
    prompt_total += agg_calls * AGGREGATE_MAX_CHARS // 4  # 卡片拼接实际远小于全文上限
    output_total = (len(input_chars_list) + agg_calls) * 600
    est: dict[str, Any] = {
        "model": model,
        "calls": len(input_chars_list) + agg_calls,
        "prompt_chars_total": prompt_total,
        "output_chars_total": output_total,
        "est_prompt_tokens": int(prompt_total / cost.CHARS_PER_TOKEN),
        "est_output_tokens": int(output_total / cost.CHARS_PER_TOKEN),
        "basis": "craft_inputs",
        "basis_note": "章级按正文实际字数、卷/全书按卡片拼接的保守上限",
    }
    price = cost.model_price(registry, model)
    if price is None:
        est.update({"priced": False, "est_cost": None,
                    "price_note": "反代通道不产生账单（成本是账号风控）"})
    else:
        money = (est["est_prompt_tokens"] / 1000.0 * price["in_per_1k"]
                 + est["est_output_tokens"] / 1000.0 * price["out_per_1k"])
        est.update({
            "priced": True, "est_cost": round(money, 4),
            "price_per_1k_in": price["in_per_1k"],
            "price_per_1k_out": price["out_per_1k"],
            "price_provider": price["provider"],
        })
    est["budget_limit"] = budget
    est["spent_before"] = round(max(0.0, float(spent_before)), 4)
    est["projected_total"] = round(est["spent_before"] + (est.get("est_cost") or 0.0), 4)
    est["over_budget"] = (
        bool(est.get("priced")) and budget > 0 and est["projected_total"] > budget)
    return est
