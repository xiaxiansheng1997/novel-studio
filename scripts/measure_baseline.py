"""M0 基线实测：只调用现有 /v1/chat/completions，不碰业务代码、不写库。

测什么、为什么测：
- length     各站点单次能吞下多长的 prompt（决定 Assembler 的总预算）
- speed      3000 字草稿的真实生成速度（校准流水线 ETA）
- stability  连续 N 次请求的失败率与风控触发（决定重试与冷却参数）
- json       结构化抽取的 JSON 合法率（决定图谱抽取要不要加重试层）
- material   长素材注入后是否编造来源（决定 S_src 桶能不能给到 6k）

用法：
    .venv\\Scripts\\python.exe -m scripts.measure_baseline --dry-run
    .venv\\Scripts\\python.exe -m scripts.measure_baseline --suite length
    .venv\\Scripts\\python.exe -m scripts.measure_baseline --suite all --runs 3

结果：
    storage/m0/raw_<时间戳>.jsonl  逐条原始记录（可中断、可续跑）
    storage/m0/report_<时间戳>.md  汇总报告

前置条件：后端已启动，且对应 provider 已登录（/live 里登录一次即可）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "storage" / "m0"

# 默认三家各取一个主力模型；模型名即 provider 前缀路由
DEFAULT_MODELS = "deepseek-chat,doubao-pro,qwen-max"
DEFAULT_SIZES = "8000,16000,28000"

# 长度测试用「首尾双探针」：开头一个、90% 处一个，两个都答对才说明真的读了全文。
# 单探针的缺陷：只测末尾无法区分「全文读完」与「只读了前 8k 但恰好答对」。
# 另：实测豆包会只答数字（漏前缀），所以每个探针分 exact/partial 两档判定。
FRONT_CODE = "ZP-2048"
FRONT_DIGITS = "2048"
TAIL_CODE = "QX-7391"
TAIL_DIGITS = "7391"
FRONT_LINE = f"本卷资料的内部编号是 {FRONT_CODE}。"
TAIL_LINE = f"第 42 号档案的封存代码是 {TAIL_CODE}。"

FILLER = (
    "雨停之后，青石巷里的水洼映着灰白的天光。少年提着一盏旧灯，沿墙根慢慢往前走，"
    "灯罩上有细密的水汽，模糊了火苗的轮廓。巷子尽头传来打更的梆子声，一声长一声短，"
    "像是有人在黑暗里试探着什么。他停下脚步，忽然听见墙后有人低声说话，声音很轻，"
    "却又格外清楚。风从巷口吹进来，卷起地上的枯叶，也把那些断断续续的字句吹散了一半。"
)


# ============================ 基础设施 ============================


def load_env() -> None:
    """把 .env 里的 W2A_* 读进 os.environ（不覆盖已存在的值）。"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def resolve_base_url() -> str:
    host = os.getenv("W2A_HOST", "127.0.0.1")
    port = os.getenv("W2A_PORT", "8000")
    return f"http://{host}:{port}"


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Recorder:
    """逐条追加写 jsonl，跑一半中断也不丢数据。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def chat_once(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    prompt: str,
    *,
    timeout: float,
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    """发一次流式请求，返回正文/首字延迟/总耗时。

    强制 history_mode=prompt：每次新开站点会话，避免上一轮上下文污染测量结果。
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "history_mode": "prompt",
        "mode": "quick",
    }
    if extra:
        body.update(extra)

    t0 = time.perf_counter()
    first: Optional[float] = None
    parts: list[str] = []
    error: Optional[str] = None

    try:
        async with client.stream(
            "POST", f"{base_url}/v1/chat/completions", json=body, timeout=timeout
        ) as resp:
            if resp.status_code != 200:
                raw = await resp.aread()
                error = f"HTTP {resp.status_code}: {raw[:400].decode('utf-8', 'replace')}"
                return {"ok": False, "error": error, "elapsed_sec": time.perf_counter() - t0}
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "error" in obj:
                    err = obj["error"]
                    error = str(err.get("message") if isinstance(err, dict) else err)
                    break
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                chunk = delta.get("content")
                if chunk:
                    if first is None:
                        first = time.perf_counter() - t0
                    parts.append(chunk)
    except (httpx.TimeoutException, httpx.ReadError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - 单次测量失败不该中断整轮
        error = f"{type(exc).__name__}: {exc}"

    text = "".join(parts)
    elapsed = time.perf_counter() - t0
    return {
        "ok": error is None and bool(text),
        "text": text,
        "output_chars": len(text),
        "first_sec": round(first, 2) if first is not None else None,
        "elapsed_sec": round(elapsed, 2),
        "error": error,
    }


# ============================ prompt 构造 ============================


def build_length_prompt(target_chars: int) -> str:
    """填充到目标长度，首尾各埋一个探针，末尾要求两项都答。

    前探针在第一段，后探针在约 90% 处：
    - 两个都中 → 全文读到
    - 只中前一个 → 尾部被静默截断
    - 只中后一个 → 几乎不可能（说明前探针被改写）
    """
    body: list[str] = []
    tail_at = int(target_chars * 0.9)
    size = 0
    i = 0
    while size < target_chars:
        block = f"【片段 {i + 1}】{FILLER}"
        if i == 0:
            block = f"【开篇】{FRONT_LINE}" + block
        if size < tail_at <= size + len(block):
            block += TAIL_LINE
        body.append(block)
        size += len(block)
        i += 1

    head = "下面是一份长篇设定资料，请通读后回答问题。\n\n"
    tail = (
        "\n\n问题：1) 本卷资料的内部编号是什么？2) 第 42 号档案的封存代码是什么？\n"
        "严格按下面两行输出，不要解释、不要额外内容：\n编号=...\n代码=..."
    )
    return head + "\n".join(body) + tail


def build_speed_prompt(words: int) -> str:
    return (
        "你是一位网络小说作者。请根据下面的章节纲要写一章正文。\n\n"
        "【章节】第 12 章 · 雨夜叩门\n"
        "【纲要】\n"
        "1. 主角在破庙避雨，听见门外有人叩门三下；\n"
        "2. 开门后是一名浑身湿透的女修，怀里抱着一个上锁的木匣；\n"
        "3. 女修只说了「替我保管三日」便昏倒；\n"
        "4. 主角发现木匣上有宗门印记，正是三日前灭门的那一家；\n"
        "5. 章末：庙外传来马蹄声，火把的光映在窗纸上。\n\n"
        f"【要求】约 {words} 字，第三人称，多写动作与细节，少写心理独白；"
        "结尾留钩子，不要总结。直接输出正文，不要写标题。"
    )


def build_json_prompt() -> str:
    return (
        "阅读下面这段小说片段，抽取实体、关系与事件，严格输出 JSON，不要任何解释文字。\n\n"
        "【片段】\n"
        "林昭推门而入时，玄阳真人正跪坐在蒲团上，面前的青铜灯已经熄灭。"
        "老人没有回头，只把一枚黑色玉牌推到案几边缘：「拿着它，去青云宗找你师叔苏婉。」"
        "林昭伸手去接，指尖刚碰到玉牌，整间屋子忽然剧烈震动，瓦片簌簌落下。"
        "玄阳真人猛地起身，将他推向窗外：「走！不要回头！」\n\n"
        "【输出格式】\n"
        "{\n"
        '  "entities": [{"name": "...", "kind": "character|location|item|faction|event|rule"}],\n'
        '  "relations": [{"src": "...", "type": "师徒|亲属|敌对|持有|位于|参与", "dst": "..."}],\n'
        '  "events": [{"summary": "...", "participants": ["..."], "importance": 1}\n'
        "}\n"
        "只输出 JSON。"
    )


def build_material_prompt(src_chars: int) -> tuple[str, list[str]]:
    """构造带编号的素材，返回 (prompt, 真实存在的编号列表)。

    素材里埋 3 条可核验事实，要求回答时引用编号。
    检查两件事：有没有引用真实编号；有没有编造不存在的编号。
    """
    facts = [
        ("S1", "该框架 2.0 于 2026 年 9 月 17 日发布，首个支持 SQLite 后端的正式版本。"),
        ("S2", "2.0 版本的二进制体积约 30MB，内存常驻约 250MB。"),
        ("S3", "官方文档说明 512 维向量在 4,300 条规模下，单次检索耗时约 2-5 毫秒。"),
        ("S4", "该团队在 2025 年曾尝试过 FAISS，后因部署复杂度放弃。"),
        ("S5", "2.0 的默认批大小为 32，可用参数覆盖。"),
        ("S6", "已知问题：Windows 下首次加载模型需要 1-3 秒。"),
    ]
    filler_blocks: list[str] = []
    size = 0
    i = 0
    while size < src_chars:
        block = f"[素材填充 {i + 1}] {FILLER}"
        filler_blocks.append(block)
        size += len(block)
        i += 1

    lines = [f"[{code}] {text}" for code, text in facts]
    head = "以下是与某技术方案相关的素材，编号为 S1 起。只能依据素材回答。\n\n"
    tail = (
        "\n\n问题：请用 200 字介绍该方案的版本、体积与检索性能，"
        "每个结论后面用方括号标注来源编号，例如 [S1]。"
        "不要使用素材之外的信息，不要编造编号。"
    )
    prompt = head + "\n".join(lines + filler_blocks) + tail
    return prompt, [c for c, _ in facts]


# ============================ 五个测量套件 ============================


async def suite_length(client, base_url, models, sizes, timeout, rec) -> None:
    for model in models:
        for size in sizes:
            prompt = build_length_prompt(size)
            log(f"[length] {model} 目标 {size} 字符（实际 {len(prompt)}）…")
            res = await chat_once(client, base_url, model, prompt, timeout=timeout)
            text = res.get("text") or ""

            def grade(code: str, digits: str) -> str:
                if code in text:
                    return "exact"
                return "partial" if digits in text else "none"

            front = grade(FRONT_CODE, FRONT_DIGITS)
            tail = grade(TAIL_CODE, TAIL_DIGITS)
            # 只有两个都命中才算「全文读到」；只中前者说明尾部被截
            full = front != "none" and tail != "none"
            rec.add({
                "suite": "length",
                "model": model,
                "target_chars": size,
                "prompt_chars": len(prompt),
                "ok": bool(res.get("ok")),
                "front_match": front,
                "tail_match": tail,
                "needle_found": full,
                "answer": text[:160],
                "first_sec": res.get("first_sec"),
                "elapsed_sec": res.get("elapsed_sec"),
                "error": res.get("error"),
            })
            if full:
                flag = "全文读到"
            elif front != "none" and tail == "none":
                flag = "尾部被截"
            elif front == "none" and tail != "none":
                flag = "仅尾部命中"
            else:
                flag = "两个都没读到"
            log(f"[length] {model} {size}: {flag} 首字={res.get('first_sec')}s 总={res.get('elapsed_sec')}s")


async def suite_speed(client, base_url, models, runs, words, timeout, rec) -> None:
    for model in models:
        for i in range(runs):
            prompt = build_speed_prompt(words)
            log(f"[speed] {model} 第 {i + 1}/{runs} 次，目标 {words} 字…")
            res = await chat_once(client, base_url, model, prompt, timeout=timeout)
            chars = res.get("output_chars") or 0
            first = res.get("first_sec")
            elapsed = res.get("elapsed_sec") or 0
            # 生成速率要扣掉首字等待，否则会被“排队+思考”摊薄
            gen_time = max(elapsed - (first or 0), 0.1)
            rate = round(chars / gen_time, 1) if res.get("ok") else None
            rec.add({
                "suite": "speed",
                "model": model,
                "target_words": words,
                "output_chars": chars,
                "first_sec": first,
                "elapsed_sec": elapsed,
                "chars_per_sec": rate,
                "ok": bool(res.get("ok")),
                "error": res.get("error"),
            })
            log(f"[speed] {model}: {chars} 字 / {elapsed}s = {rate} 字每秒")


async def suite_stability(client, base_url, models, runs, timeout, rec) -> None:
    for model in models:
        for i in range(runs):
            prompt = build_speed_prompt(600)  # 短一点，只测稳定性不测速度
            log(f"[stability] {model} 第 {i + 1}/{runs} 次…")
            res = await chat_once(client, base_url, model, prompt, timeout=timeout)
            rec.add({
                "suite": "stability",
                "model": model,
                "run": i + 1,
                "ok": bool(res.get("ok")),
                "output_chars": res.get("output_chars"),
                "elapsed_sec": res.get("elapsed_sec"),
                "error": res.get("error"),
            })
            log(f"[stability] {model} #{i + 1}: ok={bool(res.get('ok'))} {res.get('error') or ''}")


async def suite_json(client, base_url, models, runs, timeout, rec) -> None:
    prompt = build_json_prompt()
    for model in models:
        for i in range(runs):
            log(f"[json] {model} 第 {i + 1}/{runs} 次…")
            res = await chat_once(client, base_url, model, prompt, timeout=timeout)
            text = (res.get("text") or "").strip()
            valid = False
            fields_ok = False
            parsed: Any = None
            # 模型常把 JSON 包在 ```json 里，先剥一层再判
            candidate = text
            if "```" in candidate:
                seg = candidate.split("```")
                candidate = max(seg, key=len).strip()
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
            try:
                parsed = json.loads(candidate)
                valid = True
                fields_ok = all(k in parsed for k in ("entities", "relations", "events"))
            except json.JSONDecodeError as exc:
                parsed = {"parse_error": str(exc)}
            rec.add({
                "suite": "json",
                "model": model,
                "run": i + 1,
                "valid_json": valid,
                "fields_ok": fields_ok,
                "entities": len(parsed.get("entities") or []) if isinstance(parsed, dict) else 0,
                "raw": text[:300],
                "ok": bool(res.get("ok")),
                "error": res.get("error"),
            })
            log(f"[json] {model}: valid={valid} fields={fields_ok}")


async def suite_material(client, base_url, models, src_chars, timeout, rec) -> None:
    prompt, real_codes = build_material_prompt(src_chars)
    log(f"[material] 素材 prompt {len(prompt)} 字符，真实编号 {real_codes}")
    for model in models:
        res = await chat_once(client, base_url, model, prompt, timeout=timeout)
        text = res.get("text") or ""
        cited = sorted({f"S{n}" for n in range(1, 13) if f"S{n}" in text})
        fabricated = [c for c in cited if c not in real_codes]
        rec.add({
            "suite": "material",
            "model": model,
            "prompt_chars": len(prompt),
            "cited": cited,
            "fabricated": fabricated,
            "answer": text[:400],
            "ok": bool(res.get("ok")) and not fabricated,
            "error": res.get("error"),
        })
        log(f"[material] {model}: 引用={cited} 编造={fabricated or '无'}")


# ============================ 报告 ============================


def write_report(records: list[dict], path: Path) -> None:
    lines = [f"# M0 基线实测报告", "", f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}", ""]

    length = [r for r in records if r["suite"] == "length"]
    if length:
        lines += ["## 1. prompt 长度上限", "", "| 模型 | 目标字符 | 实际字符 | 开头探针 | 末尾探针 | 结论 | 首字(s) | 总耗时(s) |", "|---|---|---|---|---|---|---|---|"]
        grade_cn = {"exact": "中", "partial": "部分", "none": "未中"}
        for r in length:
            if r.get("needle_found"):
                verdict = "全文读到"
            elif r.get("front_match", "none") != "none":
                verdict = "尾部被截"
            elif r.get("tail_match", "none") != "none":
                verdict = "仅尾部命中"
            else:
                verdict = "异常/失败"
            lines.append(
                f"| {r['model']} | {r['target_chars']} | {r['prompt_chars']} | "
                f"{grade_cn.get(r.get('front_match'), '-')} | {grade_cn.get(r.get('tail_match'), '-')} | "
                f"{verdict} | {r.get('first_sec')} | {r.get('elapsed_sec')} |"
            )
        lines.append("")

    speed = [r for r in records if r["suite"] == "speed" and r.get("ok")]
    if speed:
        lines += ["## 2. 生成速度", "", "| 模型 | 输出字数 | 首字(s) | 总耗时(s) | 字/秒 |", "|---|---|---|---|---|"]
        by_model: dict[str, list[dict]] = {}
        for r in speed:
            by_model.setdefault(r["model"], []).append(r)
        for model, rows in by_model.items():
            rates = [r["chars_per_sec"] for r in rows if r.get("chars_per_sec")]
            med = statistics.median(rates) if rates else 0
            avg_chars = int(statistics.mean([r["output_chars"] for r in rows]))
            avg_total = statistics.mean([r["elapsed_sec"] for r in rows])
            lines.append(f"| {model} | {avg_chars} | {rows[0].get('first_sec')} | {avg_total:.1f} | {med} |")
        lines.append("")
        # 直出结论：按中位速率反推 3000 字需要多久
        for model, rows in by_model.items():
            rates = [r["chars_per_sec"] for r in rows if r.get("chars_per_sec")]
            if rates:
                med = statistics.median(rates)
                lines.append(f"- {model}：3000 字约需 {3000 / med:.0f} 秒（按中位 {med} 字/秒）")
        lines.append("")

    stab = [r for r in records if r["suite"] == "stability"]
    if stab:
        lines += ["## 3. 连续稳定性", "", "| 模型 | 成功 | 总次数 | 成功率 | 平均耗时(s) |", "|---|---|---|---|---|"]
        by_model = {}
        for r in stab:
            by_model.setdefault(r["model"], []).append(r)
        for model, rows in by_model.items():
            ok = sum(1 for r in rows if r.get("ok"))
            avg = statistics.mean([r.get("elapsed_sec") or 0 for r in rows])
            lines.append(f"| {model} | {ok} | {len(rows)} | {ok / len(rows) * 100:.0f}% | {avg:.1f} |")
        errs = [r.get("error") for r in stab if r.get("error")]
        if errs:
            lines += ["", "失败样本：", ""]
            for e in errs[:5]:
                lines.append(f"- `{str(e)[:160]}`")
        lines.append("")

    js = [r for r in records if r["suite"] == "json"]
    if js:
        lines += ["## 4. 结构化 JSON 抽取", "", "| 模型 | 合法 JSON | 字段完整 | 实体数 |", "|---|---|---|---|"]
        by_model = {}
        for r in js:
            by_model.setdefault(r["model"], []).append(r)
        for model, rows in by_model.items():
            v = sum(1 for r in rows if r.get("valid_json"))
            f = sum(1 for r in rows if r.get("fields_ok"))
            ents = int(statistics.mean([r.get("entities") or 0 for r in rows]))
            lines.append(f"| {model} | {v}/{len(rows)} | {f}/{len(rows)} | {ents} |")
        lines.append("")

    mat = [r for r in records if r["suite"] == "material"]
    if mat:
        lines += ["## 5. 长素材注入", "", "| 模型 | prompt 字符 | 引用编号 | 编造编号 | 结论 |", "|---|---|---|---|---|"]
        for r in mat:
            verdict = "通过" if r.get("ok") else "有问题"
            lines.append(
                f"| {r['model']} | {r['prompt_chars']} | {','.join(r.get('cited') or [])} | "
                f"{','.join(r.get('fabricated') or []) or '无'} | {verdict} |"
            )
        lines.append("")

    lines += [
        "## 6. 下一步",
        "",
        "1. 若某模型在某档长度「未读到末尾」，该档以下取一档作为默认预算。",
        "2. 用上面的字/秒中位数替换文档 10.3 节的估算值。",
        "3. 稳定性成功率低于 80% 时，先调 `min_interval_sec` 与 `W2A_RISK_COOLDOWN`，再谈批量。",
        "4. JSON 合法率低于 90% 时，抽取层必须加重试与修复。",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ============================ 入口 ============================


async def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description="M0 基线实测（只调 /v1，不碰业务代码）")
    ap.add_argument("--suite", default="all",
                    choices=["length", "speed", "stability", "json", "material", "all"])
    ap.add_argument("--models", default=DEFAULT_MODELS, help="逗号分隔，默认三家各一个")
    ap.add_argument("--sizes", default=DEFAULT_SIZES, help="length 套件的字符档位")
    ap.add_argument("--runs", type=int, default=3, help="speed/json/stability 的重复次数")
    ap.add_argument("--words", type=int, default=3000, help="speed 套件的目标字数")
    ap.add_argument("--src-chars", type=int, default=12000, help="material 套件的素材字符数")
    ap.add_argument("--timeout", type=float, default=900.0, help="单次请求读超时（秒）")
    ap.add_argument("--base-url", default="", help="默认按 .env 拼 http://host:port")
    ap.add_argument("--api-key", default="", help="默认取 W2A_API_KEY")
    ap.add_argument("--dry-run", action="store_true", help="只构造 prompt 看长度，不发请求")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    base_url = args.base_url or resolve_base_url()
    api_key = args.api_key or os.getenv("W2A_API_KEY", "")

    if args.dry_run:
        print("=== dry-run：只构造 prompt，不发请求 ===")
        for size in sizes:
            p = build_length_prompt(size)
            print(f"length  target={size:>6}  actual={len(p):>6}")
        print(f"speed   {len(build_speed_prompt(args.words))} 字符，要求 {args.words} 字")
        print(f"json    {len(build_json_prompt())} 字符")
        mp, codes = build_material_prompt(args.src_chars)
        print(f"material {len(mp)} 字符，真实编号 {codes}")
        print(f"base_url={base_url} models={models}")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = now_tag()
    rec = Recorder(OUT_DIR / f"raw_{tag}.jsonl")
    log(f"base_url={base_url} models={models} 输出={rec.path}")

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    limits = httpx.Limits(max_connections=2, max_keepalive_connections=1)
    timeout = httpx.Timeout(connect=15.0, read=args.timeout, write=120.0, pool=15.0)

    async with httpx.AsyncClient(headers=headers, limits=limits, timeout=timeout) as client:
        # 先探活：服务没起或口令不对时，别让用户等十分钟才看到第一个报错
        try:
            r = await client.get(f"{base_url}/health", timeout=10)
            log(f"/health -> {r.status_code} {r.text[:120]}")
        except Exception as exc:  # noqa: BLE001
            log(f"无法连接后端 {base_url}: {exc}")
            return 2

        suites = ["length", "speed", "stability", "json", "material"] if args.suite == "all" else [args.suite]
        for name in suites:
            log(f"===== 开始 {name} =====")
            if name == "length":
                await suite_length(client, base_url, models, sizes, args.timeout, rec)
            elif name == "speed":
                await suite_speed(client, base_url, models, args.runs, args.words, args.timeout, rec)
            elif name == "stability":
                await suite_stability(client, base_url, models, args.runs, args.timeout, rec)
            elif name == "json":
                await suite_json(client, base_url, models, args.runs, args.timeout, rec)
            elif name == "material":
                await suite_material(client, base_url, models, args.src_chars, args.timeout, rec)

    records = [json.loads(l) for l in rec.path.read_text(encoding="utf-8").splitlines() if l.strip()]
    report = OUT_DIR / f"report_{tag}.md"
    write_report(records, report)
    log(f"报告已写入 {report}")
    print(report.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 老终端不支持就算了
            pass
    raise SystemExit(asyncio.run(main()))
