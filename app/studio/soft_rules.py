"""软规则：称呼漂移、设定冲突、绝对化表述、数字无出处。

与硬规则（`consistency.py`）的分工：

| | 硬规则 | 软规则（本模块） |
|---|---|---|
| 判据 | 结构上必然错（死人复活、时间倒退） | 八成有问题，但可能作者就是这么想的 |
| 级别 | error / warn | 一律 **info** |
| 打分 | 参与「设定合规」扣分 | **不参与扣分**（`scoring._score_compliance` 只数 error/warn） |
| 定稿 | 建议清零 | 不阻塞 |

为什么不参与扣分：软规则靠字面模式猜，误报率不可能为零。一旦进扣分链路，
批量跑 50 章会被这些噪音拖低分数，人就会开始无视分数——那比漏报更糟。
原设计写的是「软规则需 LLM 二次确认才报」，实现时改成**人确认**：
零调用成本，且报出来的措辞永远是「请确认是不是同一人」而不是「你写错了」。

四条规则的取舍（都为了压误报，宁可漏报）：

1. `address_drift`：不用 NER，改为**用已有实体反查**。只对「已知人物的姓/名 + 常见称谓」
   这个封闭集合做字面匹配，所以报出来的串必然与某个已有角色同姓同名根，不会把
   「武林人士」当成「林某」。要求同姓只对应一个已知人物，且该称呼出现 ≥2 次或
   紧跟句读（明确在当称呼用）。
2. `setting_conflict`：只判**年龄**——「越写越年轻」几乎不可能是故意的，而身份/地位/
   距离这类词需要语义才能对齐，交给人。跨节对账在整本检查时跑（要读全书正文）。
3. `absolute_claim`：模板里早就引用了这条（tpl_lifestyle），一直没实现。
4. `number_unsourced`：正文里的具体数字，在本作品的素材/事实卡里一个字符都对不上。
   素材库为空时整条跳过——没登记素材就报「全部数字无出处」只会让人关掉告警。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# 每条规则每节最多报几条：软规则刷屏比漏报更讨人嫌
MAX_PER_RULE = 3

SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵季贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支"
    "柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程嵇邢"
    "扫裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗山"
    "谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸司"
    "韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴鬱胥能苍双闻莘党"
    "翟谭贡逢公仆郁危商"
)
COMPOUND_SURNAMES = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟",
                     "公孙", "长孙", "慕容", "令狐", "宇文", "轩辕", "南宫")

# 常见称谓后缀。只用于「已知姓 + 这些后缀」的组合枚举，不单独做正则，
# 所以列表长一点也不会增加误报面。
TITLES = (
    "公子 小姐 少爷 姑娘 夫人 太太 老爷 大人 先生 女士 老师 师父 师傅 道长 将军 "
    "校尉 统领 捕头 知府 知县 县令 郡主 王妃 太子 殿下 神父 大夫 掌柜 老板 队长 "
    "头领 寨主 门主 谷主 岛主 城主 少主 长老 掌门 尊者 天师 总管 管家 师兄 师姐 "
    "师弟 师妹 前辈 阁下 兄台 大叔 大爷 嫂子 媳妇 局长 处长 科长 主任 经理 律师 "
    "教授 博士 警官 探长 队长".split()
)
# 名字尾常用的叠称/小称：{前缀}{尾字} 或 {尾字}{后缀}
NAME_PREFIXES = ("阿", "小", "老")
NAME_SUFFIXES = ("哥", "姐", "叔", "婶", "伯", "爷", "儿", "兄弟", "妹妹", "弟弟")

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")
PUNCT_AFTER = "，。！？；：、,.;:!?”’）) "

AGE_RE = re.compile(r"([〇零一二两三四五六七八九十百千廿卅\d]{1,6})\s*岁")
CN_NUM = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000,
          "廿": 20, "卅": 30}
AGE_MIN, AGE_MAX = 1, 200

ABSOLUTE_RE = re.compile(
    r"(最好的|最佳|最强|最全|最优|性价比最高|最便宜"
    # 裸「第一」不收：「第一步」「第一类」「第一，输入 xxx」是序号不是吹嘘，误报率太高
    r"|唯一一家|第一名|第一位|首创|全球第一|全国第一|世界(?:第一|最大)|销量第一|排名第一|行业第一"
    # 裸「唯一」同样不收：「这是唯一的缺点」「它是后续沟通的唯一凭据」是普通用法。
    # 真实正文里扫一遍，这个规则的误报全部来自裸「唯一」。
    r"|唯一(?:选择|值得|一家|正品|渠道|推荐|方案|指定|购买)"
    r"|绝无仅有|无与伦比|100\s*%|百分之百|百分百|稳赚不赔|零风险|包治"
    r"|必然(?:涨|赚|成功)|无一例外)"
)
# 只认「带单位或带年」的数字。裸数字太常见了——HTTP 码、P99、版本号、步骤号，
# 报出来全是噪音；而真该有出处的数据（规模、增速、份额）都带单位。
# 前面的否定后顾是用来挡住 P99 / v1.2 这种「字母贴在数字前」的写法。
NUM_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z.])"
    r"((?:19|20)\d{2}\s*年"
    r"|\d+(?:[.,]\d+)*\s*(?:%|％|个百分点|倍|秒|小时|天|ms|MB|GB|KB|TB"
    r"|(?:万|亿)?(?:元|人|台|辆|套|吨|人次)))"
)

CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


# ==================== 小工具 ====================

def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    try:
        got = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(x) for x in got if x] if isinstance(got, list) else []


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text or "") if s.strip()]


def _strip_code(text: str) -> str:
    """代码块与行内代码换成等长空白。

    等技术类正文里有大量数字（超时 2000ms、503、1.18.0），它们不是「数据」，
    不该要求出处。保持长度不变是为了证据坐标不漂。
    """
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    return INLINE_CODE_RE.sub(blank, CODE_FENCE_RE.sub(blank, text or ""))


def cn_to_int(raw: str) -> Optional[int]:
    """支持纯数字与中文数字（二十三 / 廿七 / 十五 / 35）。

    两类歧义直接不认，宁可不报：

    - 「六七八九」这种没单位的连写（不是任何正常写法）
    - 「一百二」——102 还是 120？口语里是 120，字面上是 102
    """
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = num = 0
    seen_unit = False
    last_unit = 0
    for ch in s:
        val = CN_NUM.get(ch)
        if val is None:
            return None
        if val >= 10:            # 十 / 廿 / 卅 / 百 / 千
            total += (num or 1) * val
            num = 0
            seen_unit = True
            last_unit = val
        else:
            num = val
    if not seen_unit:
        return num if len(s) == 1 and num else None
    if num and last_unit >= 100:
        return None
    return total + num or None


def _clean_hits(content: str, form: str) -> list[tuple[int, bool]]:
    """找出 `form` 的「干净出现」位置。

    返回 [(位置, 后面紧跟句读)]。只要求**前一个字符不是汉字**——这一条同时挡住
    「武林公子」（前字「武」）和「他叫林公子」（前字「叫」），漏报换误报归零。
    后面接汉字不管：「林公子起身」是正常的主语用法，挡了它就等于本条规则废掉。
    真正的尾部歧义（「林公司」）靠的是称谓表里没有「公」这个后缀，不靠窗口。
    """
    out: list[tuple[int, bool]] = []
    if not form:
        return out
    start = 0
    n = len(content)
    while True:
        i = content.find(form, start)
        if i < 0:
            break
        start = i + 1
        prev = content[i - 1] if i else ""
        if prev and CJK_RE.match(prev):
            continue
        j = i + len(form)
        nxt = content[j] if j < n else ""
        out.append((i, bool(nxt and nxt in PUNCT_AFTER)))
    return out


def _surname_of(name: str) -> str:
    for cp in COMPOUND_SURNAMES:
        if name.startswith(cp) and len(name) > len(cp):
            return cp
    if name and name[0] in SURNAMES:
        return name[0]
    return ""


def _snippet(content: str, pos: int, form: str, pad: int = 26) -> str:
    lo = max(0, pos - pad)
    hi = min(len(content), pos + len(form) + pad)
    return content[lo:hi].replace("\n", " ").strip()


# ==================== 1. 称呼漂移 ====================

def _character_pool(entities: list[dict], cards: list[dict]) -> dict[str, dict]:
    """name/alias -> 该角色的规范信息。只有人物参与称呼漂移判定。"""
    pool: dict[str, dict] = {}
    for row in entities:
        if (row.get("kind") or "") != "character":
            continue
        names = [row["name"]] + _json_list(row.get("aliases"))
        info = {
            "name": row["name"], "id": row["id"], "status": row.get("status") or "alive",
            "card_id": row.get("card_id") or "", "aliases": _json_list(row.get("aliases")),
        }
        for nm in names:
            if len(nm) >= 2:
                pool.setdefault(nm, info)
    for c in cards:
        if (c.get("kind") or "") != "character":
            continue
        info = {
            "name": c["name"], "id": f"card:{c['id']}", "status": "alive",
            "card_id": c["id"], "aliases": _json_list(c.get("aliases")),
            "card_age": _json_dict(c.get("data_json")).get("age"),
            "card_ordinal": _json_dict(c.get("data_json")).get("status_ordinal") or 0,
        }
        for nm in [c["name"]] + info["aliases"]:
            if len(nm) >= 2:
                pool.setdefault(nm, info)
    return pool


def _address_forms(info: dict, pool: dict[str, dict]) -> list[str]:
    """这个人物可能被怎么称呼：姓+称谓、名+尾称。只枚举，不猜。"""
    name = info["name"]
    forms: list[str] = []
    surname = _surname_of(name)
    known = {name, *info["aliases"]}
    # 同姓的已知人物有几个？两个以上就不判（不知道「林公子」指谁）
    same_surname = {i["name"] for i in pool.values() if surname and i["name"].startswith(surname)}
    if surname and len(same_surname) == 1:
        forms += [surname + t for t in TITLES]
    given = name[len(surname):] if surname and len(name) > len(surname) else name[1:]
    if given:
        tail = given[-1]
        forms += [p + tail for p in NAME_PREFIXES] + [tail + s for s in NAME_SUFFIXES]
        if len(given) > 1:
            forms.append("阿" + given)
    return [f for f in forms if len(f) >= 2 and f not in known]


async def check_address_drift(store, work: dict, node: dict, content: str,
                              ordinal: int) -> list[dict]:
    """正文里用了某个已知人物的新称呼，但别名表里没有 → 提醒加别名或补新卡。"""
    entities = await store.list_entities(work["id"])
    cards = await store.list_cards(work["id"])
    pool = _character_pool(entities, cards)
    if not pool:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for info in {i["name"]: i for i in pool.values()}.values():
        for form in _address_forms(info, pool):
            if form in seen:
                continue
            hits = _clean_hits(content, form)
            if not hits:
                continue
            # ≥2 次，或者 1 次但紧跟句读（「林公子，里边请。」这种明确在当称呼用）
            if len(hits) < 2 and not hits[0][1]:
                continue
            seen.add(form)
            pos = hits[0][0]
            out.append({
                "rule": "address_drift", "severity": "info",
                "message": (f"本节 {len(hits)} 次用「{form}」指代，它和「{info['name']}」同姓名根，"
                            f"但不在「{info['name']}」的别名表里。是同一人就加别名，不是就补张设定卡"),
                "evidence": _snippet(content, pos, form),
                "location": str(ordinal),
            })
            if len(out) >= MAX_PER_RULE:
                return out
    return out


# ==================== 2. 设定冲突（年龄） ====================

def _age_mentions(content: str, pool: dict[str, dict]) -> list[tuple[str, int, str, int]]:
    """句中只有一个已知人物锚点时才采信那个岁数。返回 (人名, 岁数, 证据, 位置)。"""
    out: list[tuple[str, int, str, int]] = []
    for sent in _sentences(content):
        for m in AGE_RE.finditer(sent):
            age = cn_to_int(m.group(1))
            if age is None or not (AGE_MIN <= age <= AGE_MAX):
                continue
            anchors = {info["name"] for nm, info in pool.items() if nm in sent}
            if len(anchors) != 1:
                continue  # 一句里两个人，或一个都没有：不猜
            name = next(iter(anchors))
            pos = (content.find(sent[:20]) if content.find(sent[:20]) >= 0 else 0) + m.start()
            out.append((name, age, sent[:160], pos))
    return out


async def check_age_against_card(store, work: dict, node: dict, content: str,
                                 ordinal: int) -> list[dict]:
    """正文岁数比设定卡的权威值还小 → 冲突（变大是过日子，变小不可能）。"""
    cards = await store.list_cards(work["id"])
    pool = _character_pool(await store.list_entities(work["id"]), cards)
    if not pool:
        return []
    card_age: dict[str, tuple[int, int]] = {}
    for c in cards:
        data = _json_dict(c.get("data_json"))
        raw = data.get("age")
        try:
            age = int(raw)
        except (TypeError, ValueError):
            continue
        card_age[c["name"]] = (age, int(data.get("status_ordinal") or 0))
        for alias in _json_list(c.get("aliases")):
            card_age[alias] = (age, int(data.get("status_ordinal") or 0))

    out: list[dict] = []
    for name, age, evidence, _ in _age_mentions(content, pool):
        pair = card_age.get(name)
        if not pair:
            continue
        want, since = pair
        if ordinal and ordinal < since:
            continue  # 卡片那个值是「从第 N 节起」，之前的章节不归它管
        if age >= want:
            continue
        if len(out) >= MAX_PER_RULE:
            break
        out.append({
            "rule": "setting_conflict", "severity": "info",
            "message": (f"设定卡里 {name} 是 {want} 岁，本节写成 {age} 岁"
                        f"（{ordinal and f'第 {ordinal} 节'}）——变年轻基本不可能是故意的"),
            "evidence": evidence,
            "location": str(ordinal),
        })
    return out


async def check_age_across_nodes(store, work: dict) -> list[dict]:
    """整本检查才跑：跨节年龄单调性。挂在「变年轻那一节」的节点上，便于定位。"""
    entities = await store.list_entities(work["id"])
    cards = await store.list_cards(work["id"])
    pool = _character_pool(entities, cards)
    if not pool:
        return []

    history: dict[str, list[tuple[int, int, str]]] = {}
    nodes: dict[int, str] = {}
    for item in await store.list_readable(work["id"]):
        node = await store.get_node(item["id"])
        if not node:
            continue
        content = (node.get("content") or "").strip()
        if len(content) < 40:
            continue
        ordinal = int(node.get("ordinal") or 0)
        nodes[ordinal] = node["id"]
        for name, age, evidence, _ in _age_mentions(content, pool):
            history.setdefault(name, []).append((ordinal, age, evidence))

    out: list[dict] = []
    for name, rows in history.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r[0])
        # 同节内先给出两个不同岁数，其次跨节变小
        by_ordinal: dict[int, list[int]] = {}
        for ordinal, age, ev in rows:
            by_ordinal.setdefault(ordinal, []).append(age)
        for ordinal, ages in sorted(by_ordinal.items()):
            if len(set(ages)) > 1:
                out.append({
                    "rule": "setting_conflict", "severity": "info",
                    "message": (f"{name} 在同一节里出现了 {sorted(set(ages))[0]} 岁与 "
                                f"{sorted(set(ages))[-1]} 岁两种说法"),
                    "evidence": next((e for o, _, e in rows if o == ordinal), ""),
                    "location": str(ordinal), "node_id": nodes.get(ordinal),
                })
        prev_age = prev_ord = None
        for ordinal, age, evidence in rows:
            if prev_age is not None and age < prev_age:
                out.append({
                    "rule": "setting_conflict", "severity": "info",
                    "message": (f"{name} 第 {prev_ord} 节写 {prev_age} 岁，第 {ordinal} 节变成 {age} 岁"
                                "——中间没人倒着长"),
                    "evidence": evidence,
                    "location": str(ordinal), "node_id": nodes.get(ordinal),
                })
            if prev_age is None or age > prev_age:
                prev_age, prev_ord = age, ordinal
        if len(out) >= MAX_PER_RULE * 3:
            break
    return out


# ==================== 3. 绝对化表述 ====================

async def check_absolute_claim(store, work: dict, node: dict, content: str,
                               ordinal: int) -> list[dict]:
    out: list[dict] = []
    for sent in _sentences(_strip_code(content)):
        m = ABSOLUTE_RE.search(sent)
        if not m:
            continue
        if len(out) >= MAX_PER_RULE:
            break
        out.append({
            "rule": "absolute_claim", "severity": "info",
            "message": (f"「{m.group(1)}」是绝对化表述，推荐类文章这么写会被当成广告；"
                        "改成「在我的预算里最合适」这类有条件说法"),
            "evidence": sent[:160],
            "location": str(ordinal),
        })
    return out


# ==================== 4. 数字无出处 ====================

def _digits_only(raw: str) -> str:
    return re.sub(r"[^0-9]", "", raw or "")


# 素材正文拼成的「数字池」。整本检查时每节都要查一次，逐条 get_source 太浪费，
# 于是按「来源/事实卡的 id 与字数」做指纹缓存。指纹没变就复用；
# 增删素材或改动正文字数都会变指纹，唯一漏掉的是「改内容但字数不变」，
# 那种情况重启服务即可，不值得为它引入失效通知。
_SOURCE_CACHE: dict[str, tuple[str, str]] = {}
CACHE_MAX_WORKS = 32
SOURCE_READ_LIMIT = 60


def reset_source_cache() -> None:
    """测试或重载配置时手动清一下。"""
    _SOURCE_CACHE.clear()


async def _source_corpus(store, work_id: str) -> str:
    """本作品全部素材 + 事实卡里的数字（只剩 0-9 的裸串）。"""
    sources = await store.list_sources(work_id, limit=SOURCE_READ_LIMIT)
    facts = await store.list_facts(work_id, limit=500)
    if not sources and not facts:
        return ""
    signature = "|".join(
        [f"{s['id']}:{s.get('chars') or 0}" for s in sources]
        + [f"f{f['id']}" for f in facts]
    )
    cached = _SOURCE_CACHE.get(work_id)
    if cached and cached[0] == signature:
        return cached[1]
    # list_sources 不返回 content（只返回 length(content) AS chars），正文要单独取
    parts = [f.get("statement") or "" for f in facts]
    for row in sources:
        full = await store.get_source(row["id"])
        if full:
            parts.append(full.get("content") or "")
    digits = _digits_only(" ".join(parts))
    if len(_SOURCE_CACHE) >= CACHE_MAX_WORKS:
        _SOURCE_CACHE.clear()
    _SOURCE_CACHE[work_id] = (signature, digits)
    return digits


async def check_number_unsourced(store, work: dict, node: dict, content: str,
                                 ordinal: int) -> list[dict]:
    """正文里的具体数字，在素材/事实卡里一个都对不上。

    素材与事实卡都是空的就整条跳过：那时报「全部无出处」只会逼人关掉告警。
    判定是「数字串能不能在素材里找到」，做的是子串匹配 —— 宽进严出，
    素材里写过 2027 年就算 27 有出处。宁可漏报。
    """
    corpus = await _source_corpus(store, work["id"])
    if not corpus:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for sent in _sentences(_strip_code(content)):
        for m in NUM_TOKEN_RE.finditer(sent):
            token = m.group(1)
            digits = _digits_only(token)
            if len(digits) < 2 or digits in seen:
                continue
            # 年份里去掉「年」字后仍是 4 位数字，直接按数字查
            if digits in corpus:
                continue
            seen.add(digits)
            if len(out) >= MAX_PER_RULE:
                return out
            out.append({
                "rule": "number_unsourced", "severity": "info",
                "message": (f"正文里的「{token.strip()}」在本作品的素材与事实卡中都找不到，"
                            "确认一下是笔误还是漏登记来源"),
                "evidence": sent[:160],
                "location": str(ordinal),
            })
    return out


# ==================== 入口 ====================

NOVEL_SOFT = ("address_drift", "setting_conflict")
DOC_SOFT = ("absolute_claim", "number_unsourced")


async def check_node(store, work: dict, node: dict, content: str, ordinal: int,
                     rules: set[str]) -> list[dict]:
    """单节软规则。只跑 `resolved["checks"]` 里显式开着的。"""
    is_novel = (work.get("kind") or "article") == "novel"
    out: list[dict] = []
    if is_novel:
        if "address_drift" in rules:
            out += await check_address_drift(store, work, node, content, ordinal)
        if "setting_conflict" in rules:
            out += await check_age_against_card(store, work, node, content, ordinal)
    else:
        if "absolute_claim" in rules:
            out += await check_absolute_claim(store, work, node, content, ordinal)
        if "number_unsourced" in rules:
            out += await check_number_unsourced(store, work, node, content, ordinal)
    return out


async def check_whole_book(store, work: dict, rules: set[str]) -> list[dict]:
    """整本才跑得起的软规则（跨节年龄对账）。"""
    if (work.get("kind") or "article") != "novel" or "setting_conflict" not in rules:
        return []
    return await check_age_across_nodes(store, work)
