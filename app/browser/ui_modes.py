"""网页端模式对齐：发送前把页面的「模型下拉 / 深度思考 / 联网」切到目标状态。

站点 DOM 一直在变，所以定位不写死 CSS 选择器，而是：
- 按按钮文案（config.yaml 的 ui.labels 可覆盖）在可见元素里找最内层命中项；
- 状态优先读 aria-pressed / aria-checked，其次看类名正则，最后才看 ui.blind；
- 状态读不出来时不盲点（避免把已开的点成关），直接记 missing 交给 /live 校准。

对外只有两个函数：
- probe_ui(page)：列出输入框一带的可点控件与当前状态，供 /live 探测面板使用；
- apply_mode(page, key, plan, cfg)：按 ModePlan 切模式，返回 applied/missing，不抛异常。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from loguru import logger

from app.browser.stream_capture import dismiss_blocking_dialogs
from app.config import UiControlConfig
from app.core.modes import ModePlan

# 找到元素后打的临时标记，供 Playwright 用真实鼠标事件点击
_ATTR = "data-w2a-mode"

# 模型切换失败后 10 分钟内不重复尝试：文案没校准前每次请求都点一遍下拉很浪费
_MODEL_RETRY_SEC = 600

_TOGGLES_JS = r"""
(args) => {
  const { labels, mark, activeRe } = args;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && r.bottom > 0 && r.top < innerHeight;
  };
  document.querySelectorAll('[' + mark + ']').forEach(e => e.removeAttribute(mark));
  const re = activeRe ? new RegExp(activeRe, 'i') : null;
  const sel = "button,[role='button'],[role='switch'],[role='checkbox'],[role='radio']," +
              "label,[aria-pressed],[aria-checked],[tabindex]";
  const hits = [];
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue;
    const t = norm(el.innerText || el.getAttribute('aria-label') || el.title || '');
    if (!t || t.length > 30) continue;
    let score = 0;
    for (const lb of labels) {
      const L = norm(lb);
      if (!L) continue;
      if (t === L) score = Math.max(score, 3);
      else if (t.includes(L)) score = Math.max(score, 2);
    }
    if (!score) continue;
    hits.push({ el, t, score });
  }
  if (!hits.length) return { found: false };
  // 分数高者优先；同分取文本最短的，避免命中整条工具栏
  hits.sort((a, b) => (b.score - a.score) || (a.t.length - b.t.length));
  const el = hits[0].el;
  el.setAttribute(mark, '1');
  const pressed = el.getAttribute('aria-pressed');
  const checked = el.getAttribute('aria-checked');
  const cls = String(el.className || '');
  let state = null;
  let via = '';
  if (pressed === 'true' || checked === 'true') { state = true; via = pressed !== null ? 'aria-pressed' : 'aria-checked'; }
  else if (pressed === 'false' || checked === 'false') { state = false; via = pressed !== null ? 'aria-pressed' : 'aria-checked'; }
  else if (re) { state = re.test(cls); via = 'class'; }
  return { found: true, text: hits[0].t, state, via, cls: cls.slice(0, 120) };
}
"""

# 「已选中/触发器」只在长得很像控件的元素里判断：扫 div,span 或整页 body 会把
# 正文里提到的模型名（“上一条用的是 Qwen3-Max”）误判成已切换，从此永远不切模型
_CAND_SEL = (
    "button,[role='button'],[role='combobox'],[role='listbox'],[aria-haspopup],"
    "label,summary,[class*='model'],[class*='Model'],[class*='select'],[class*='Select'],"
    "[class*='trigger'],[class*='Trigger']"
)

_MODEL_JS = r"""
(args) => {
  const { desired, triggerLabels, knownLabels, mark, activeRe, CAND } = args;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && r.bottom > 0 && r.top < innerHeight;
  };
  document.querySelectorAll('[' + mark + ']').forEach(e => e.removeAttribute(mark));
  const re = activeRe ? new RegExp(activeRe, 'i') : null;
  const cands = [];
  for (const el of document.querySelectorAll(CAND)) {
    if (!vis(el)) continue;
    const t = norm(el.innerText || el.getAttribute('aria-label') || el.title || '');
    if (!t || t.length > 40) continue;
    cands.push({ el, t });
  }
  for (const c of cands) {
    if (c.t.includes(desired)) return { found: true, shown: true, text: c.t };
  }
  // 触发器：写着别的已知模型名的 > 文案命中的 > 类名命中的
  const KN = (knownLabels || []).map(norm).filter(Boolean);
  const scored = [];
  for (const c of cands) {
    let score = 0;
    if (KN.some(k => c.t.includes(k))) score = 4;
    for (const lb of (triggerLabels || [])) {
      const L = norm(lb);
      if (!L) continue;
      if (c.t === L) score = Math.max(score, 3);
      else if (c.t.includes(L)) score = Math.max(score, 2);
    }
    if (re && re.test(String(c.el.className || ''))) score = Math.max(score, 1);
    if (score) scored.push({ el: c.el, t: c.t, score });
  }
  if (!scored.length) return { found: false, shown: false };
  scored.sort((a, b) => (b.score - a.score) || (a.t.length - b.t.length));
  scored[0].el.setAttribute(mark, '1');
  return { found: true, shown: false, trigger: scored[0].t };
}
"""

# 点后复核：与 _MODEL_JS 同口径，只看控件型短文本元素
_SHOWN_JS = r"""
(args) => {
  const { desired, cand } = args;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && r.bottom > 0 && r.top < innerHeight;
  };
  for (const el of document.querySelectorAll(cand)) {
    if (!vis(el)) continue;
    const t = norm(el.innerText || el.getAttribute('aria-label') || '');
    if (t && t.length <= 40 && t.includes(desired)) return true;
  }
  return false;
}
"""

_ITEM_JS = r"""
(args) => {
  const { desired, mark } = args;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && r.bottom > 0 && r.top < innerHeight;
  };
  document.querySelectorAll('[' + mark + ']').forEach(e => e.removeAttribute(mark));
  // 限定在展开的菜单/浮层里找，避免点到页面别处的同名文字
  const scopes = Array.from(document.querySelectorAll(
    "[role='menu'],[role='listbox'],[role='dialog'],[class*='dropdown'],[class*='Dropdown']," +
    "[class*='menu'],[class*='Menu'],[class*='popover'],[class*='Popover']"
  )).filter(vis);
  const pool = [];
  for (const s of scopes) {
    pool.push(...s.querySelectorAll("[role='menuitem'],[role='option'],li,div,button,span"));
  }
  if (!pool.length) {
    pool.push(...document.querySelectorAll("[role='menuitem'],[role='option'],li,button"));
  }
  const hits = [];
  for (const el of pool) {
    if (!vis(el)) continue;
    const t = norm(el.innerText || '');
    if (!t || t.length > 40) continue;
    if (t === desired) hits.push({ el, len: t.length });
    else if (t.includes(desired) && t.length <= desired.length + 12) hits.push({ el, len: t.length });
  }
  if (!hits.length) return { found: false };
  hits.sort((a, b) => a.len - b.len);
  hits[0].el.setAttribute(mark, '1');
  return { found: true, text: norm(hits[0].el.innerText || '') };
}
"""

_PROBE_JS = r"""
(args) => {
  const wanted = ((args && args.wanted) || []).map(s => (s || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && r.bottom > 0 && r.top < innerHeight;
  };
  const sel = "button,[role='button'],[role='switch'],[role='checkbox'],[role='radio']," +
              "[aria-pressed],[aria-checked],[class*='toggle'],[class*='switch']," +
              "[class*='mode'],[class*='Mode'],[aria-haspopup]";
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const t = norm(el.innerText || el.getAttribute('aria-label') || el.title || '');
    if (!t || t.length > 24) continue;
    // 已知文案（模型名/开关名）无条件保留：模型下拉常在页面顶部，按输入框区域筛会漏
    const hit = wanted.some(w => w === t || t.includes(w) || (w.length > 3 && w.includes(t)));
    if (!hit && r.top < innerHeight * 0.3) continue;
    out.push({
      text: t,
      role: el.getAttribute('role') || '',
      pressed: el.getAttribute('aria-pressed'),
      checked: el.getAttribute('aria-checked'),
      cls: String(el.className || '').slice(0, 100),
      known: hit ? 1 : 0,
    });
    if (out.length >= 60) break;
  }
  out.sort((a, b) => (b.known - a.known));
  return out;
}
"""


async def probe_ui(page, wanted: Optional[list[str]] = None) -> list[dict]:
    """列出可点控件（文本 + 状态），用于 /live 校准。

    wanted 传已声明的模型名/开关文案：这些控件常在页面顶部，不能只探输入框一带。
    """
    try:
        raw = await page.evaluate(_PROBE_JS, {"wanted": list(wanted or [])})
    except Exception as exc:  # noqa: BLE001 - 页面切换中读不到 DOM 很常见
        logger.debug(f"[ui] 探测控件失败: {exc}")
        return []
    seen: set[tuple] = set()
    out: list[dict] = []
    for it in raw or []:
        sig = (it.get("text"), it.get("pressed"), it.get("checked"), it.get("cls"))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(it)
    return out


async def _click_mark(page, value: str = "1") -> bool:
    """用真实鼠标事件点击打了标记的元素；失败再退化成 JS click。"""
    loc = page.locator(f"[{_ATTR}='{value}']").first
    try:
        await loc.click(timeout=3000)
        return True
    except Exception as exc:  # noqa: BLE001 - 元素被遮挡/动画中，退化点击
        logger.debug(f"[ui] 定位点击失败，退化 JS click: {exc}")
    try:
        return bool(
            await page.evaluate(
                "(sel) => { const el = document.querySelector(sel); if (!el) return false; el.click(); return true; }",
                f"[{_ATTR}='{value}']",
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[ui] JS click 失败: {exc}")
        return False


async def _set_toggle(page, feature: str, want: bool, ctl: UiControlConfig) -> dict:
    """把一个开关切到目标状态；已处于目标状态时不点击。"""
    labels = ctl.labels or [feature]
    args = {"labels": labels, "mark": _ATTR, "activeRe": ctl.active_class or ""}
    try:
        seen = await page.evaluate(_TOGGLES_JS, args)
    except Exception as exc:  # noqa: BLE001
        return {"feature": feature, "want": want, "ok": False, "reason": f"读取页面失败: {exc}"}

    if not seen or not seen.get("found"):
        return {"feature": feature, "want": want, "ok": False, "reason": "按钮未找到", "labels": labels}

    text = seen.get("text", "")
    state = seen.get("state")
    if state is None and not ctl.blind:
        return {
            "feature": feature,
            "want": want,
            "ok": False,
            "text": text,
            "reason": "状态不可判定（请在 /live 探测后配 ui.active_class，或设 blind: true）",
        }
    if state == want:
        return {"feature": feature, "want": want, "ok": True, "action": "无需改动", "text": text}

    if not await _click_mark(page):
        return {"feature": feature, "want": want, "ok": False, "text": text, "reason": "点击失败"}
    await page.wait_for_timeout(400)

    try:
        after = await page.evaluate(_TOGGLES_JS, args)
    except Exception:  # noqa: BLE001
        after = {}
    now = (after or {}).get("state")
    ok = True if now is None and ctl.blind else now == want
    return {
        "feature": feature,
        "want": want,
        "ok": ok,
        "action": "已点击",
        "text": (after or {}).get("text", text),
        "state_after": now,
        "reason": "" if ok else f"点击后状态仍为 {now}",
    }


async def _ensure_model(
    page, desired: str, ctl: Optional[UiControlConfig], trigger_fallback: list[str]
) -> dict:
    """把模型下拉切到 desired（网页端下拉里显示的那串文案）。"""
    ctl = ctl or UiControlConfig()
    fail = getattr(page, "_w2a_model_fail", None)
    if fail and fail.get("label") == desired and time.time() - fail.get("at", 0) < _MODEL_RETRY_SEC:
        return {
            "feature": "model",
            "want": desired,
            "ok": False,
            "reason": "上次切换失败，10 分钟内不再重试（请用 /live 校准模型文案）",
        }

    trigger_labels = list(ctl.labels or []) + list(trigger_fallback)
    args = {
        "desired": desired,
        "triggerLabels": trigger_labels,
        "knownLabels": list(trigger_fallback or []),
        "mark": _ATTR,
        "activeRe": ctl.active_class or "",
        "CAND": _CAND_SEL,
    }
    try:
        seen = await page.evaluate(_MODEL_JS, args)
    except Exception as exc:  # noqa: BLE001
        return {"feature": "model", "want": desired, "ok": False, "reason": f"读取页面失败: {exc}"}

    if not seen or not seen.get("found"):
        page._w2a_model_fail = {"label": desired, "at": time.time()}
        return {
            "feature": "model",
            "want": desired,
            "ok": False,
            "reason": "没找到模型下拉（请用 /live 探测后配 ui.model.labels）",
            "labels": trigger_labels,
        }
    if seen.get("shown"):
        return {"feature": "model", "want": desired, "ok": True, "action": "无需改动", "text": desired}

    if not await _click_mark(page):
        page._w2a_model_fail = {"label": desired, "at": time.time()}
        return {"feature": "model", "want": desired, "ok": False, "reason": "模型下拉打不开"}
    await page.wait_for_timeout(500)

    try:
        item = await page.evaluate(_ITEM_JS, {"desired": desired, "mark": _ATTR})
    except Exception as exc:  # noqa: BLE001
        item = {"found": False, "reason": str(exc)}
    if not item or not item.get("found"):
        # 关掉可能已经打开的菜单，避免影响后面的输入
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        page._w2a_model_fail = {"label": desired, "at": time.time()}
        return {"feature": "model", "want": desired, "ok": False, "reason": "下拉里没有这个文案的选项"}

    if not await _click_mark(page):
        page._w2a_model_fail = {"label": desired, "at": time.time()}
        return {"feature": "model", "want": desired, "ok": False, "reason": "选项点击失败"}
    await page.wait_for_timeout(500)

    try:
        shown = bool(await page.evaluate(_SHOWN_JS, {"desired": desired, "cand": _CAND_SEL}))
    except Exception:  # noqa: BLE001
        shown = False
    if shown:
        page._w2a_model_fail = None
        return {"feature": "model", "want": desired, "ok": True, "action": "已切换", "text": desired}

    page._w2a_model_fail = {"label": desired, "at": time.time()}
    return {"feature": "model", "want": desired, "ok": False, "reason": "点了选项但页面没显示该模型"}


async def apply_mode(page, key: str, plan: ModePlan, cfg: Any) -> dict:
    """把页面切到 ModePlan 描述的状态；任何失败都只降级返回，不抛异常。"""
    started = time.perf_counter()
    details: list[dict] = []
    missing: list[str] = []

    try:
        await dismiss_blocking_dialogs(page, key)
    except Exception:  # noqa: BLE001 - 弹窗清理失败不影响后续判断
        pass

    for feature, want in plan.changed_features.items():
        ctl = (cfg.ui or {}).get(feature)
        if ctl is None:
            missing.append(feature)
            details.append({"feature": feature, "want": want, "ok": False, "reason": "provider 未配置 ui"})
            continue
        res = await _set_toggle(page, feature, bool(want), ctl)
        details.append(res)
        if not res.get("ok"):
            missing.append(feature)

    if plan.model_label:
        # 模型下拉的触发器通常显示“当前模型名”，所以把已声明的文案都当候选
        known_labels = [m.label for m in cfg.models if getattr(m, "label", "")]
        res = await _ensure_model(page, plan.model_label, (cfg.ui or {}).get("model"), known_labels)
        details.append(res)
        if not res.get("ok"):
            missing.append("model")

    ms = int((time.perf_counter() - started) * 1000)
    ok = not missing
    result = {
        "ok": ok,
        "missing": missing,
        "details": details,
        "preset": plan.preset_id,
        "ms": ms,
        "url": (getattr(page, "url", "") or "")[:120],
    }
    if ok:
        logger.info(f"[{key}] 模式已对齐 {plan.summary()} 用时 {ms}ms")
    else:
        logger.warning(
            f"[{key}] 模式部分未对齐 {plan.summary()} 缺失={missing} 用时 {ms}ms "
            f"（可在 /live 的「页面模式」里探测实际文案后改 config.yaml）"
        )
    return result
