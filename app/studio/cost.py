"""批量生成前的成本预估与预算上限（迭代 C）。

预估不另起炉灶：拿**这部作品自己历史 runs 的中位数**——「这本书写一节要喂
多少字、产多少字」没有比它自己上一轮更准的依据。样本不足（<3 条）退回全局
保守默认，并在返回里明说依据（basis），让界面能把「估的」和「量的」分开讲。

计价只认真金的通道：compat（官方 API）在 config.yaml 里带每千 token 单价；
网页端反代不产生账单，但它的成本是账号风控——所以照报字数，钱一栏写 null，
界面上「花多少字」和「花多少钱」本来就是两件事。

字符→token 用最粗的 2 字符比 1（中文口径）：这是预估不是账单，宁可标粗
也不假装精确。预算超限默认拦下（HTTP 402），确实要跑的人用 ignore_budget
放行——拦的是手滑，不拦清醒的决定。
"""
from __future__ import annotations

from typing import Any, Optional

# 中文混合文本的粗折算：约 2 字符 = 1 token
CHARS_PER_TOKEN = 2.0
# 没有历史样本时的保守默认（M0 实测 28k 输入是上限场景，日常装配远低于它）
DEFAULT_PROMPT_CHARS = 12000
DEFAULT_OUTPUT_CHARS = 1500
# 至少几条成功 run 才敢用它们的中位数当依据
MIN_RUNS_FOR_BASELINE = 3

BUDGET_KEY = "budget_limit"


def model_price(registry: Any, model: str) -> Optional[dict[str, float]]:
    """模型对应 provider 的千 token 单价；反代/路由不到 → None（不计钱）。"""
    try:
        adapter = registry.route(model)
    except Exception:  # noqa: BLE001 - 预估阶段路由不到不该抛
        return None
    cfg = adapter.cfg
    if not getattr(cfg, "compat", False):
        return None
    return {
        "in_per_1k": float(getattr(cfg, "price_in_per_1k_tokens", 0.0) or 0.0),
        "out_per_1k": float(getattr(cfg, "price_out_per_1k_tokens", 0.0) or 0.0),
        "provider": cfg.key,
    }


def median(nums: list[int]) -> int:
    """中位数：store.run_baselines 也用这一份，口径只修一处。"""
    if not nums:
        return 0
    xs = sorted(nums)
    mid = len(xs) // 2
    return int(xs[mid]) if len(xs) % 2 else int((xs[mid - 1] + xs[mid]) / 2)


async def budget_limit(store: Any) -> float:
    """预算上限（钱数）。0 或没配 = 不设限。"""
    raw = (await store.get_setting(BUDGET_KEY) or "").strip()
    try:
        return max(0.0, float(raw or 0))
    except ValueError:
        return 0.0


async def set_budget_limit(store: Any, limit: float) -> None:
    if limit < 0:
        raise ValueError("预算上限不能为负")
    await store.set_setting(BUDGET_KEY, f"{float(limit):g}")


async def estimate_pipeline(
    store: Any,
    registry: Any,
    *,
    work_id: str,
    sections: int,
    model: str,
) -> dict[str, Any]:
    """对「即将批量生成的 sections 节」给一份预估。纯读，不动队列不落库。"""
    base = await store.run_baselines(work_id)
    if base.get("count", 0) >= MIN_RUNS_FOR_BASELINE:
        prompt_chars = max(1, int(base["prompt_median"]))
        output_chars = max(1, int(base["output_median"]))
        basis = "runs_median"
        basis_note = f"依据本书最近 {base['count']} 次成功生成的中位数"
    else:
        prompt_chars = DEFAULT_PROMPT_CHARS
        output_chars = DEFAULT_OUTPUT_CHARS
        basis = "default"
        basis_note = "本书还没有足够的成功记录，按保守默认估算"

    prompt_total = prompt_chars * sections
    output_total = output_chars * sections
    est: dict[str, Any] = {
        "sections": sections,
        "model": model,
        "per_section_prompt_chars": prompt_chars,
        "per_section_output_chars": output_chars,
        "prompt_chars_total": prompt_total,
        "output_chars_total": output_total,
        "est_prompt_tokens": int(prompt_total / CHARS_PER_TOKEN),
        "est_output_tokens": int(output_total / CHARS_PER_TOKEN),
        "basis": basis,
        "basis_note": basis_note,
    }
    price = model_price(registry, model)
    budget = await budget_limit(store)
    if price is None:
        est.update({"priced": False, "est_cost": None, "price_note": "反代通道不产生账单（成本是账号风控）"})
    else:
        cost = (
            est["est_prompt_tokens"] / 1000.0 * price["in_per_1k"]
            + est["est_output_tokens"] / 1000.0 * price["out_per_1k"]
        )
        est.update({
            "priced": True,
            "est_cost": round(cost, 4),
            "price_per_1k_in": price["in_per_1k"],
            "price_per_1k_out": price["out_per_1k"],
            "price_provider": price["provider"],
        })
    est["budget_limit"] = budget
    est["over_budget"] = bool(est.get("priced")) and budget > 0 and (est.get("est_cost") or 0) > budget
    return est
