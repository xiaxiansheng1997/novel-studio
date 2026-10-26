"""M3 验收：图谱抽取 / 子图查询 / 5 条硬规则 / S6 注入。

用法：
    .venv\\Scripts\\python.exe -m scripts.smoke_m3
    .venv\\Scripts\\python.exe -m scripts.smoke_m3 --llm    # 额外跑一次真实抽取

思路：故意造一本「周叙已死却还在场」「玄铁令同时被两人持有」的小说。
这两件冲突不是靠模型看出来的，是靠图谱比对算出来的——所以脚本能断言。

造数据分两步：
1. 设定卡与节点走 HTTP，走用户真实路径
2. 关系/事件/伏笔直接用 sqlite 插：它们本来就只该由抽取产生，
   为此在 API 上开一个「手写关系」的口子反而污染设计
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
LOG = BASE_DIR / "storage" / "m3.log"

CH1 = """玄铁令在青岚仓库交割。林昭把木匣推过桌面，周叙抬手压住，两人的手指都没离开匣子。

「三日后子时，西市口。」周叙说完这句，把令收进袖中，转身走了。
那天的雨一直下到天亮，仓库屋檐上挂着水线，像是要断不断的帘子。

回到宅子，林昭在灯下把今日的事记了一笔：玄铁令易主，周叙执掌，三日后西市。
写完他把纸折了三折，塞进地砖下面。这个习惯是很多年前落下的，改不掉。"""

CH2 = """周叙推门进来的时候，林昭正蹲在灶前添柴。火光一跳，他抬起头，看见周叙袖口一圈深色的水渍。

玄铁令还在周叙手里，边角被磨得发亮。林昭盯着那块铁，一时没说话。

「西市口的事，」周叙把令放在灶台上，「再算一算。」

林昭想起三日前仓库屋檐上的水线，忽然觉得那场雨根本没停过。"""


def out(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def fail(msg: str) -> None:
    out("FAIL: " + msg)
    raise SystemExit(1)


def db_path() -> Path:
    raw = os.environ.get("W2A_STORAGE_DIR") or "storage"
    p = Path(raw)
    if not p.is_absolute():
        p = BASE_DIR / p
    return p / "studio.sqlite"


def sql(statement: str, params: tuple = ()) -> None:
    with sqlite3.connect(db_path(), timeout=10) as conn:
        conn.execute(statement, params)
        conn.commit()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--llm", action="store_true", help="额外跑一次真实抽取（调模型）")
    ap.add_argument("--keep", action="store_true", help="保留测试作品")
    args = ap.parse_args()

    env = BASE_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    key = os.environ.get("W2A_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=httpx.Timeout(600.0),
                                 headers=headers) as c:
        w = (await c.post("/studio/works", json={
            "kind": "novel", "scale": "single", "subtype": "fantasy",
            "title": "玄铁令", "premise": "一枚令符三人转手，谁都在说谎",
            "target_words": 20000,
        })).json()
        wid = w["id"]
        out(f"[1] work -> {wid}  kind={w['kind']} buckets={w.get('kind')}")

        n1 = (await c.post(f"/studio/works/{wid}/nodes", json={
            "kind": "chapter", "title": "雨夜交割", "content": CH1})).json()
        n2 = (await c.post(f"/studio/works/{wid}/nodes", json={
            "kind": "chapter", "title": "灶前重算", "content": CH2})).json()
        out(f"[2] nodes -> 第{n1['ordinal']}章《{n1['title']}》 / 第{n2['ordinal']}章《{n2['title']}》")

        # ---------- 设定卡：作者写「周叙已死」，代码必须当权威 ----------
        for body in (
            {"kind": "character", "name": "周叙", "summary": "漕帮账房，第 1 章之后已死",
             # status_ordinal=1：从第 1 章起死亡。没这个坐标，前面几章出场都会被当成复活
             "data_json": json.dumps({"status": "dead", "status_ordinal": 1,
                                      "note": "死于西市口"})},
            {"kind": "character", "name": "林昭", "summary": "替人抄书的落魄书生"},
            {"kind": "item", "name": "玄铁令", "summary": "漕帮信物，见令如见帮主"},
            {"kind": "location", "name": "青岚仓库", "summary": "漕帮存粮处"},
        ):
            r = await c.post(f"/studio/works/{wid}/cards", json=body)
            if r.status_code != 200:
                fail(f"建卡失败 {body['name']}: {r.status_code} {r.text[:200]}")
        out("[3] cards -> 4 张（周叙标死）")

        g = (await c.get(f"/studio/works/{wid}/graph")).json()
        ents = {e["name"]: e for e in g["entities"]}
        out(f"[4] graph -> entities={g['stats']['entities']} cards={g['stats']['cards']}")
        if "周叙" not in ents:
            fail("设定卡没有生成实体，卡片与图谱仍然两张皮")
        if ents["周叙"]["status"] != "dead":
            fail(f"周叙状态应为 dead，实际 {ents['周叙']['status']}")
        if not ents["周叙"].get("card_id"):
            out("     注意：实体没有挂上 card_id，面板上无法标注「作者定的」")

        # ---------- 人为制造关系与时间：这些本该由抽取产生 ----------
        now = time.time()
        for src, dst, rtype in (
            ("林昭", "玄铁令", "持有"),
            ("周叙", "玄铁令", "持有"),
            ("林昭", "青岚仓库", "位于"),
        ):
            sql("INSERT OR IGNORE INTO relations(id, work_id, src_id, dst_id, rel_type, "
                "since_ordinal, confidence, evidence_ordinal) VALUES(?,?,?,?,?,1,1.0,1)",
                (f"r_{wid}_{src}_{dst}", wid, ents[src]["id"], ents[dst]["id"], rtype))
        sql("INSERT INTO events(id, work_id, ordinal, summary, participants, location_id, "
            "story_time, importance) VALUES(?,?,?,?,?,?,?,2)",
            (f"ev1_{wid}", wid, int(n1["ordinal"]), "玄铁令交割", "[]", None, "2026-10-20"))
        sql("INSERT INTO events(id, work_id, ordinal, summary, participants, location_id, "
            "story_time, importance) VALUES(?,?,?,?,?,?,?,2)",
            (f"ev2_{wid}", wid, int(n2["ordinal"]), "周叙重新出现", "[]", None, "2026-10-10"))
        sql("INSERT INTO foreshadows(id, work_id, planted_ordinal, planned_payoff_ordinal, "
            "content, status, importance) VALUES(?,?,?,?,?,'open',2)",
            (f"fs_{wid}", wid, 1, 1, "地砖下那张折了三折的纸"))
        out(f"[5] 造数 -> relations=3 events=2(时间倒退) foreshadows=1(超期) {now:.0f}")
        # 直插库绕过了抽取链路，内存图缓存还是旧的；refresh 一次再把两边对齐
        await c.get(f"/studio/works/{wid}/graph", params={"refresh": True})

        # ---------- 子图查询 ----------
        sg = (await c.get(f"/studio/works/{wid}/graph/subgraph",
                          params={"text": "周叙 玄铁令", "hops": 2})).json()
        names = [e["name"] for e in sg["entities"]]
        out(f"[6] subgraph -> seeds={sg['seeds']} entities={names}")
        if "周叙" not in names:
            fail("子图没有命中种子实体")
        preview = sg.get("preview") or ""
        out(f"[7] S6 预览 -> {len(preview)} 字")
        for line in preview.splitlines()[:6]:
            out("     " + line)
        if "玄铁令" not in preview:
            fail("S6 文本里没有物品，关系没进子图")

        # ---------- 一致性检查 ----------
        r = await c.post(f"/studio/works/{wid}/check", json={"node_id": n2["id"]})
        r.raise_for_status()
        res = r.json()
        rules = sorted({i["rule"] for i in res["issues"]})
        out(f"[8] check(ch2) -> {res['created']} 条 {rules}")
        for i in res["issues"]:
            out(f"     [{i['severity']}] {i['rule']}: {i['message']}")
            if i.get("evidence"):
                out(f"        证据：{i['evidence'][:70]}")
        if "dead_character" not in rules:
            fail("周叙已死却在第 2 章推门进来，dead_character 没报")
        if "item_conflict" not in rules:
            fail("玄铁令同时被两人持有且都在场，item_conflict 没报")
        if "time_regression" not in rules:
            fail("第 2 章故事时间早于第 1 章，time_regression 没报")

        r = await c.post(f"/studio/works/{wid}/check", json={})
        r.raise_for_status()
        whole = sorted({i["rule"] for i in r.json()["issues"]})
        out(f"[9] check(整本) -> {r.json()['created']} 条 {whole}")
        if "foreshadow_overdue" not in whole:
            fail("伏笔计划在第 1 章回收却到第 2 章还没收，foreshadow_overdue 没报")
        early = [i for i in r.json()["issues"]
                 if i["rule"] == "dead_character" and i.get("node_id") == n1["id"]]
        out(f"     第 1 章 dead_character 误报 {len(early)} 条（应为 0：周叙第 1 章还活着）")
        if early:
            fail("卡片的时间坐标没生效：周叙第 1 章就被判复活")

        # ---------- 误报防护：回忆里的死人不能报 ----------
        n3 = (await c.post(f"/studio/works/{wid}/nodes", json={
            "kind": "chapter", "title": "旧事", "content":
            "林昭坐在门槛上，想起周叙。那年周叙还活着，两人在码头分了半块饼。\n\n"
            "纸钱烧到最后只剩灰，风一吹就散了。他起身拍掉衣摆上的灰，回屋去了。"})).json()
        r = await c.post(f"/studio/works/{wid}/check", json={"node_id": n3["id"]})
        r.raise_for_status()
        got = [i for i in r.json()["issues"] if i["rule"] == "dead_character"]
        out(f"[10] 误报防护 -> 回忆性提及报 {len(got)} 条（应为 0）")
        if got:
            fail(f"回忆里的周叙被当成复活：{got[0]['evidence'][:60]}")

        # ---------- S6 是否真的进了上下文 ----------
        r = await c.post(
            f"/studio/context/preview?work_id={wid}&node_id={n2['id']}",
            json={"task_type": "draft", "instruction": "写周叙夜访，气氛要压抑"},
        )
        r.raise_for_status()
        ctx = r.json()
        ids = [b["id"] for b in ctx["buckets"]]
        out(f"[11] preview -> 桶={ids} 总量={ctx['total_chars']}")
        for b in ctx["buckets"]:
            out(f"     {b['id']:<6} {b['label']:<8} {b['chars']}/{b['budget']}")
        if "S6" not in ids:
            fail("S6 桶没出现：图谱没有注入")
        if "周叙" not in (ctx["user"] or ""):
            fail("S6 里没有周叙：模型看不到人物状态")
        out("[12] 断言通过：S6 带着人物状态与未回收伏笔进入上下文")

        # ---------- 真实抽取 ----------
        if args.llm:
            r = await c.post(f"/studio/works/{wid}/graph/extract",
                             json={"node_id": n1["id"], "force": True})
            if r.status_code != 200:
                fail(f"抽取失败 {r.status_code}: {r.text[:300]}")
            ex = r.json()
            out(f"[13] extract -> {ex['elapsed_ms'] if 'elapsed_ms' in ex else ''} "
                f"entities={ex['entities']} relations={ex['relations']} "
                f"events={ex['events']} states={ex['states']} foreshadows={ex['foreshadows']}")
            if ex["failed"]:
                fail(f"抽取失败：{ex['errors']}")
            g2 = (await c.get(f"/studio/works/{wid}/graph")).json()
            out(f"[14] graph(抽取后) -> {g2['stats']}")
            if g2["stats"]["events"] <= 2:
                out("     注意：抽取没有新增事件，可能模型只回了空数组")

        # ---------- 增量：内容没变不重复抽 ----------
        if args.llm:
            r = await c.post(f"/studio/works/{wid}/graph/extract",
                             json={"node_id": n1["id"], "force": False})
            if r.status_code == 200:
                out(f"[15] extract(增量) -> skipped={r.json()['skipped']}")

        if not args.keep:
            await c.delete(f"/studio/works/{wid}")
            out("[16] 清理测试作品")

    out("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
