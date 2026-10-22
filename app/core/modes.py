"""模型/模式能力：把「模型名 + 请求参数」解析成网页端要设置的目标状态。

网页端既有「模型下拉」，也有「深度思考 / 联网 / 专家模式」这类开关。
对外采用双轨，两条路最终都收敛成同一份 ModePlan：

- 模型名：站点真实模型 id（deepseek-chat / qwen-max / doubao-pro）
  也接受模式后缀别名（`-think` / `-search` / `-expert` / `-quick`，以及
  页面上绑定出来的场景模式名，如 `-code`），例如 `deepseek-reasoner-search`
  = 深度思考 + 联网。
- 请求参数：`mode` / `thinking` / `search` / `reasoning_effort`，
  优先级高于模型名，用来给只会传 model 的客户端兜底。

provider 侧只需要声明 modes（预设）与 ui（按钮文案），驱动逻辑在
app/browser/ui_modes.py 里统一实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# 内置开关语义；除此之外，页面绑定还能加自定义场景键（如 code / translate），
# 它们由 config.yaml 的 modes[].features 声明，ModePlan 原样透传给 apply_mode。
FEATURES = ("thinking", "search")

# 内置模式名；其余模式视为「场景模式」，可用作模型名后缀与能力别名
STANDARD_MODES = ("quick", "think", "search", "expert")

# 模型名后缀 -> 预设 id
_ALIAS = {
    "quick": "quick",
    "fast": "quick",
    "think": "think",
    "thinking": "think",
    "reasoner": "think",
    "search": "search",
    "web": "search",
    "expert": "expert",
}


@dataclass
class ModePlan:
    """一次请求要落到页面上的目标状态。"""

    preset_id: str = "quick"
    features: dict[str, bool] = field(default_factory=dict)
    base_model: str = ""
    model_label: str = ""
    body_patch: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def changed_features(self) -> dict[str, bool]:
        # 不再只放行 thinking/search：场景模式（code 等）也要能到页面上开/关。
        # features 全部来自配置（quick 铺底 / 模型固有 / 模式预设），不存在凭空键。
        return dict(self.features)

    def summary(self) -> str:
        feats = " ".join(f"{k}={str(v).lower()}" for k, v in self.changed_features.items())
        return f"preset={self.preset_id} {feats}".strip()


def split_model(
    models: Iterable[Any], model: str, extra_suffixes: Iterable[str] = ()
) -> tuple[Optional[Any], list[str]]:
    """拆出「基础模型 + 模式后缀」。

    先按已声明的模型 id 做最长匹配，剩下的每一段都必须是模式后缀才算数，
    这样 `deepseek-reasoner`（已声明）与 `deepseek-chat-think`（别名）都能识别，
    而 `doubao-unknown` 不会被误判成模式。

    extra_suffixes 传 provider 声明的场景模式名（如 code），它们同样能当后缀用。
    """
    model = (model or "").strip()
    extra = {s for s in extra_suffixes if s and s not in _ALIAS}
    for m in sorted(models, key=lambda x: len(x.id), reverse=True):
        if model == m.id:
            return m, []
        if model.startswith(m.id + "-"):
            toks = model[len(m.id) + 1:].split("-")
            if toks and all(t in _ALIAS or t in extra for t in toks):
                return m, toks
    return None, []


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return None


def resolve_mode(pcfg: Any, req: Any) -> ModePlan:
    """把一次请求解析成 ModePlan（解析失败一律降级，不抛异常）。"""
    # 场景模式（如 code）也能当模型名后缀：qwen-max-code
    scenes = [n for n in pcfg.modes if n not in STANDARD_MODES]
    base, suffixes = split_model(pcfg.models, getattr(req, "model", "") or "", scenes)
    plan = ModePlan(base_model=base.id if base else "")

    # 0) 先铺底 quick 预设的开关状态：每轮请求都要把页面拉到确定状态，
    #    否则上一轮开的「深度思考/联网」会默默影响这一轮。
    #    body_patch 不在这里铺：预设切换后要能看出哪些字段真的被改过。
    seed = pcfg.modes.get("quick")
    if seed:
        plan.features.update(seed.features)

    # 1) 模型自带特性（如 deepseek-reasoner 默认开思考）
    if base is not None:
        plan.model_label = getattr(base, "label", "") or ""
        if getattr(base, "features", None):
            plan.features.update(base.features)
            if base.features.get("thinking"):
                plan.preset_id = "think"
            plan.reasons.append(f"模型 {base.id} 自带 {base.features}")

    # 2) 模型名后缀（可叠加，后者覆盖前者）
    for tok in suffixes:
        pid = _ALIAS.get(tok, tok)  # 不是内置别名就是场景模式名
        plan.preset_id = pid
        spec = pcfg.modes.get(pid)
        if spec:
            plan.features.update(spec.features)
            plan.model_label = spec.model_label or plan.model_label
        plan.reasons.append(f"后缀 -{tok} -> {pid}")

    # 3) 显式 mode 预设
    mode = (getattr(req, "mode", None) or "").strip().lower()
    if mode:
        spec = pcfg.modes.get(mode)
        if spec:
            plan.preset_id = mode
            plan.features.update(spec.features)
            plan.model_label = spec.model_label or plan.model_label
            plan.reasons.append(f"mode={mode}")
        else:
            plan.reasons.append(f"未知 mode={mode} 已忽略")

    # 4) thinking / search 显式覆盖（比 mode 更具体）
    explicit: set[str] = set()
    for name in FEATURES:
        val = _as_bool(getattr(req, name, None))
        if val is not None:
            plan.features[name] = val
            explicit.add(name)
            plan.reasons.append(f"{name}={val}")

    # 5) reasoning_effort：low 不强制关，medium/high 视为要思考
    effort = (getattr(req, "reasoning_effort", None) or "").strip().lower()
    if effort in ("medium", "high") and "thinking" not in explicit:
        plan.features["thinking"] = True
        plan.reasons.append(f"reasoning_effort={effort} -> thinking")

    # 6) OpenAI 的 web_search_options
    if getattr(req, "web_search_options", None) and "search" not in explicit:
        plan.features["search"] = True
        plan.reasons.append("web_search_options -> search")

    # 7) 预设名跟最终开关对齐：两个都开 = expert，全关 = quick，日志更好读
    if plan.features.get("thinking") and plan.features.get("search"):
        if "expert" in pcfg.modes:
            plan.preset_id = "expert"
    elif not any(plan.features.values()) and "quick" in pcfg.modes:
        plan.preset_id = "quick"

    # 8) body_patch 只取最终预设：被显式关掉的开关不能带着它的上游字段下发
    final_spec = pcfg.modes.get(plan.preset_id)
    plan.body_patch = dict(final_spec.body_patch) if final_spec else {}

    return plan


def alias_ids(pcfg: Any) -> list[str]:
    """该 provider 可用的模式别名（供 /v1/capabilities 展示）。"""
    suffixes = [s for s in ("think", "search", "expert") if s in pcfg.modes]
    # 页面绑定出来的场景模式（如 code）同样可用 <model>-<scene> 调用
    suffixes += [n for n in pcfg.modes if n not in STANDARD_MODES]
    out: list[str] = []
    for m in pcfg.models:
        for suffix in suffixes:
            out.append(f"{m.id}-{suffix}")
    return out


def capability_features(pcfg: Any) -> list[str]:
    """该 provider 对外声明的能力标签。"""
    feats: set[str] = set()
    for spec in pcfg.modes.values():
        feats.update(spec.features.keys())
    if any(getattr(m, "label", "") for m in pcfg.models):
        feats.add("models")
    return sorted(feats)
