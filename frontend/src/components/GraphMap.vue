<template>
  <div v-if="open" class="mask" @click.self="close">
    <section class="dlg" role="dialog" aria-modal="true" aria-label="设定图谱">
      <header class="dlg-head">
        <div class="head-txt">
          <h2>设定图谱</h2>
          <p class="dlg-sub">
            {{ nodes.length }}/{{ entityList.length }} 实体 ·
            {{ links.length }} 关系。拖动节点会固定它，滚轮缩放，空格处按住可平移。
          </p>
        </div>
        <div class="head-acts">
          <button class="btn btn-sm" @click="relayout">重新布局</button>
          <button class="icon-btn" title="关闭（Esc）" @click="close">
            <AppIcon name="x" :size="15" />
          </button>
        </div>
      </header>

      <div v-if="kindsPresent.length > 1" class="chips">
        <button
          v-for="k in kindsPresent"
          :key="k"
          class="chip"
          :class="{ off: hiddenKinds.has(k) }"
          @click="toggleKind(k)"
        >
          <i class="dot" :style="{ background: KIND_COLOR[k] || 'var(--text-faint)' }"></i>
          {{ KIND_LABEL[k] || k }}
          <em>{{ countByKind[k] }}</em>
        </button>
      </div>

      <div class="body">
        <svg
          ref="svgEl"
          class="stage"
          @pointerdown="onStageDown"
          @wheel.prevent="onWheel"
        >
          <g :transform="viewTransform">
            <line
              v-for="l in links"
              :key="l.id"
              class="link"
              :class="{ dead: l.expired, hi: isHiLink(l) }"
              :x1="pt(l.src_id).x"
              :y1="pt(l.src_id).y"
              :x2="pt(l.dst_id).x"
              :y2="pt(l.dst_id).y"
            />
            <text
              v-for="l in labelLinks"
              :key="'t' + l.id"
              class="link-label"
              :x="(pt(l.src_id).x + pt(l.dst_id).x) / 2"
              :y="(pt(l.src_id).y + pt(l.dst_id).y) / 2 - 3"
            >{{ l.rel_type }}</text>

            <g
              v-for="n in nodes"
              :key="n.id"
              class="node"
              :class="{ dim: isDim(n), sel: selectedId === n.id }"
              @pointerdown.stop="onNodeDown(n, $event)"
              @click.stop="selectedId = n.id"
            >
              <circle
                :cx="n.x"
                :cy="n.y"
                :r="n.r"
                :style="{ fill: KIND_COLOR[n.kind] || KIND_FALLBACK }"
                :class="{ gone: n.status !== 'alive' }"
              />
              <text class="label" :x="n.x" :y="n.y + n.r + 11">{{ n.name }}</text>
            </g>
          </g>
          <text v-if="!nodes.length" class="blank" x="50%" y="45%">
            这一批筛选条件下没有实体
          </text>
        </svg>

        <aside class="detail">
          <template v-if="selected">
            <div class="d-head">
              <span class="d-name">{{ selected.name }}</span>
              <span class="d-kind" :style="{ background: KIND_COLOR[selected.kind] || 'var(--text-faint)' }">
                {{ KIND_LABEL[selected.kind] || selected.kind }}
              </span>
            </div>
            <p v-if="selected.status !== 'alive'" class="d-status">
              {{ STATUS_LABEL[selected.status] || selected.status }}
              <em v-if="selected.status_ordinal">第 {{ selected.status_ordinal }} 节起</em>
            </p>
            <dl class="d-meta">
              <div><dt>别名</dt><dd>{{ aliasesOf(selected).join('、') || '—' }}</dd></div>
              <div><dt>首次出现</dt><dd>{{ selected.first_ordinal ? '第 ' + selected.first_ordinal + ' 节' : '—' }}</dd></div>
              <div><dt>关系数</dt><dd>{{ degreeOf(selected.id) }}</dd></div>
            </dl>
            <p v-if="cardOf(selected)" class="d-card">{{ cardOf(selected).summary }}</p>
            <p v-else-if="selected.card_id" class="d-card faint">有设定卡但没写摘要</p>

            <ul class="d-rels">
              <li v-for="r in relsOf(selected.id)" :key="r.id">
                <span class="r-other">{{ otherName(r, selected.id) }}</span>
                <span class="r-type">{{ r.rel_type }}</span>
                <span v-if="r.expired" class="r-dead">已失效</span>
                <span v-else-if="r.since_ordinal" class="r-since">第{{ r.since_ordinal }}节</span>
              </li>
            </ul>

            <button class="btn btn-sm" @click="emit('locate', selected.name)">
              在正文里找这个名字
            </button>
          </template>

          <template v-else>
            <p class="d-tip">
              点一个节点看它的人物关系。虚线是已经失效的关系，灰圈是已死/已毁——
              这两种正是「死人复活」类告警的输入。
            </p>
            <ul class="legend">
              <li v-for="k in kindsPresent" :key="k">
                <i class="dot" :style="{ background: KIND_COLOR[k] || 'var(--text-faint)' }"></i>
                {{ KIND_LABEL[k] || k }}
                <em>{{ countByKind[k] }}</em>
              </li>
            </ul>
            <p v-if="truncated" class="d-tip faint">
              实体超过 {{ MAX_NODES }} 个，只画关系最多的前 {{ MAX_NODES }} 个（图太密就没法看了）。
            </p>
          </template>
        </aside>
      </div>
    </section>
  </div>
</template>

<script setup>
/**
 * 图谱的力导向视图。
 *
 * 为什么不引 d3 / cytoscape：这个图最多百来个节点，需要的只有「谁和谁连着、
 * 谁已经死了、哪条关系过期了」，一个 60 行的斥力+弹簧就够；多一个依赖
 * 就多一份打包体积和升级负担。布局是同步跑的，结果可复现（同一份数据
 * 每次打开是同一个初始环形），不做随机撒点。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  graph: { type: Object, default: null },
})
const emit = defineEmits(['update:open', 'locate'])

const KIND_LABEL = {
  character: '人物', location: '地点', item: '物品',
  faction: '势力', rule: '规则', event: '事件', foreshadow: '伏笔',
}
const STATUS_LABEL = { dead: '已死亡', destroyed: '已毁', missing: '下落不明' }
// 类别色一律引用主题 token，不再自带十六进制：换主题时图的图例跟着变。
// 注意这些值是 CSS 变量，只能进 style（SVG 的 fill 属性不认 var()）。
const KIND_COLOR = {
  character: 'var(--run)',
  location: 'var(--info)',
  item: 'var(--iris)',
  faction: 'var(--teal)',
  rule: 'var(--text-muted)',
  event: 'var(--warn)',
  foreshadow: 'var(--text-faint)',
}
const KIND_FALLBACK = 'var(--text-faint)'

// 力导向参数：调过的组合是「一团能看清的网」而不是「一个完美的圆」
const REPULSE = 7200
const SPRING_LEN = 104
const SPRING_K = 0.022
const GRAVITY = 0.006
const DAMP = 0.82
const MAX_NODES = 160

const svgEl = ref(null)
const nodes = ref([])
const links = ref([])
const selectedId = ref('')
const hiddenKinds = ref(new Set())
const view = ref({ k: 1, tx: 0, ty: 0 })
let alpha = 0
let raf = 0
let pendingFit = false
let size = { w: 900, h: 600 }
// 位置缓存：关掉再打开不该重新炸一次布局
const posCache = new Map()

const entityList = computed(() => props.graph?.entities || [])
const cardList = computed(() => props.graph?.cards || [])

const countByKind = computed(() => {
  const out = {}
  for (const e of entityList.value) out[e.kind] = (out[e.kind] || 0) + 1
  return out
})
const kindsPresent = computed(() => {
  const order = ['character', 'location', 'item', 'faction', 'rule', 'event', 'foreshadow']
  return Object.keys(countByKind.value)
    .filter((k) => countByKind.value[k])
    .sort((a, b) => order.indexOf(a) - order.indexOf(b))
})

const degree = computed(() => {
  const d = {}
  for (const r of props.graph?.relations || []) {
    d[r.src_id] = (d[r.src_id] || 0) + 1
    d[r.dst_id] = (d[r.dst_id] || 0) + 1
  }
  return d
})

const truncated = computed(() => entityList.value.length > MAX_NODES)

// ==================== 布局数据 ====================
//
// nodes / links 是普通 ref，不是 computed：力的模拟要逐帧写 x/y，
// 而 computed 不能依赖自己产出的对象。数据变化（抽取、筛选、重开）
// 时才重建，重建时从 posCache 拿回上次的位置，避免图重新炸开。

function visibleEntities() {
  const hidden = hiddenKinds.value
  let rows = entityList.value.filter((e) => !hidden.has(e.kind))
  if (rows.length > MAX_NODES) {
    const deg = degree.value
    rows = [...rows].sort((a, b) => (deg[b.id] || 0) - (deg[a.id] || 0)).slice(0, MAX_NODES)
  }
  return rows
}

function buildNodes() {
  const deg = degree.value
  return visibleEntities().map((e) => {
    const cached = posCache.get(e.id)
    return {
      id: e.id,
      name: e.name,
      kind: e.kind,
      status: e.status || 'alive',
      card_id: e.card_id || '',
      aliases: e.aliases,
      first_ordinal: e.first_ordinal || 0,
      status_ordinal: e.status_ordinal || 0,
      deg: deg[e.id] || 0,
      r: 6.5 + Math.min(9, (deg[e.id] || 0) * 1.4),
      x: cached?.x ?? 0,
      y: cached?.y ?? 0,
      vx: 0,
      vy: 0,
      pinned: !!cached?.pinned,
    }
  })
}

function buildLinks(rows) {
  const ids = new Set(rows.map((n) => n.id))
  return (props.graph?.relations || [])
    .filter((r) => r.src_id !== r.dst_id && ids.has(r.src_id) && ids.has(r.dst_id))
    .map((r) => ({
      id: r.id, src_id: r.src_id, dst_id: r.dst_id, rel_type: r.rel_type,
      src_name: r.src_name, dst_name: r.dst_name, since_ordinal: r.since_ordinal || 0,
      expired: r.until_ordinal != null,
    }))
}

const nodeById = computed(() => new Map(nodes.value.map((n) => [n.id, n])))

/** 模板里取坐标：必须经由 reactive 的 nodes 元素读 x/y，否则逐帧重画不会触发。 */
function pt(id) {
  return nodeById.value.get(id) || { x: 0, y: 0 }
}

const selected = computed(() => nodes.value.find((n) => n.id === selectedId.value) || null)
const neighborIds = computed(() => {
  if (!selectedId.value) return new Set()
  const out = new Set([selectedId.value])
  for (const l of links.value) {
    if (l.src_id === selectedId.value) out.add(l.dst_id)
    if (l.dst_id === selectedId.value) out.add(l.src_id)
  }
  return out
})
const labelLinks = computed(() =>
  selectedId.value ? links.value.filter((l) => isHiLink(l)) : []
)

function isHiLink(l) {
  return selectedId.value && (l.src_id === selectedId.value || l.dst_id === selectedId.value)
}
function isDim(n) {
  return selectedId.value ? !neighborIds.value.has(n.id) : false
}
function aliasesOf(n) {
  const raw = n.aliases
  if (Array.isArray(raw)) return raw
  try {
    return JSON.parse(raw || '[]')
  } catch {
    return []
  }
}
function degreeOf(id) {
  return degree.value[id] || 0
}
function cardOf(n) {
  return cardList.value.find((c) => c.id === n.card_id || c.name === n.name) || null
}
function relsOf(id) {
  return links.value
    .filter((l) => l.src_id === id || l.dst_id === id)
}
function otherName(r, id) {
  return r.src_id === id ? r.dst_name : r.src_name
}

// ==================== 布局 ====================

const viewTransform = computed(
  () => `translate(${view.value.tx} ${view.value.ty}) scale(${view.value.k})`
)

function measure() {
  const el = svgEl.value
  if (!el) return
  const w = el.clientWidth || 900
  const h = el.clientHeight || 600
  size = { w, h }
}

function seed() {
  measure()
  const list = nodes.value
  const n = Math.max(list.length, 1)
  const cx = size.w / 2
  const cy = size.h / 2
  const r0 = Math.min(size.w, size.h) / 2.7
  list.forEach((row, i) => {
    if (!posCache.has(row.id)) {
      // 黄金角螺旋：均匀且可复现，不像随机撒点那样每次打开位置都不同
      const ang = i * 2.399963229728644
      const r = r0 * Math.sqrt((i + 0.5) / n)
      posCache.set(row.id, { x: cx + r * Math.cos(ang), y: cy + r * Math.sin(ang), pinned: false })
    }
    const p = posCache.get(row.id)
    row.x = p.x
    row.y = p.y
    row.pinned = p.pinned
  })
}

function layout(reset = false) {
  if (reset) {
    posCache.clear()
  }
  nodes.value = buildNodes()
  links.value = buildLinks(nodes.value)
  seed()
  pendingFit = true
  alpha = reset ? 1 : 0.55
  stop()
  if (prefersReduced()) {
    // 不做动画也要收敛到稳定位置，否则节点全挤在原点
    for (let i = 0; i < 320; i++) {
      tick()
      if (alpha <= 0.02) break
    }
    commit()
    fit()
    return
  }
  raf = requestAnimationFrame(frame)
}

function prefersReduced() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

function frame() {
  for (let i = 0; i < 2; i++) tick()          // 一帧走两步，收敛快一点
  commit()
  if (alpha > 0.02) raf = requestAnimationFrame(frame)
  else {
    raf = 0
    if (pendingFit) fit()
  }
}

/**
 * 把图缩放到刚好好看：每次布局收敛后做一次。
 * 不做的后果很具体——实体一多，节点铺到画布外，打开看到半张图。
 */
function fit() {
  pendingFit = false
  const ns = nodes.value
  if (!ns.length) {
    view.value = { k: 1, tx: 0, ty: 0 }
    return
  }
  const pad = 44
  const xs = ns.map((n) => n.x)
  const ys = ns.map((n) => n.y)
  const minX = Math.min(...xs) - pad
  const maxX = Math.max(...xs) + pad
  const minY = Math.min(...ys) - pad
  const maxY = Math.max(...ys) + pad
  const k = Math.min(1.6, Math.max(0.35, Math.min(size.w / (maxX - minX), size.h / (maxY - minY))))
  view.value = {
    k,
    tx: size.w / 2 - ((minX + maxX) / 2) * k,
    ty: size.h / 2 - ((minY + maxY) / 2) * k,
  }
}

function stop() {
  if (raf) cancelAnimationFrame(raf)
  raf = 0
}

function tick() {
  const ns = nodes.value
  const ls = links.value
  if (!ns.length) return
  const at = nodeById.value
  const cx = size.w / 2
  const cy = size.h / 2
  for (let i = 0; i < ns.length; i++) {
    const a = ns[i]
    for (let j = i + 1; j < ns.length; j++) {
      const b = ns[j]
      let dx = b.x - a.x
      let dy = b.y - a.y
      let d2 = dx * dx + dy * dy
      if (d2 < 0.01) {
        // 完全重合同时给一个沿索引方向的微偏移，不靠随机数（要可复现）
        dx = (i - j) * 0.05 + 0.3
        dy = (j - i) * 0.04 + 0.2
        d2 = dx * dx + dy * dy
      }
      if (d2 > 160000) continue               // 400px 以外不斥力：省算力也防止互推飞散
      // 距离下限 8px：两个节点重叠时斥力会算出几千，一帧就把图炸飞
      const d = Math.max(Math.sqrt(d2), 8)
      const f = (REPULSE / (d * d)) * alpha
      const ux = dx / d
      const uy = dy / d
      a.vx -= ux * f
      a.vy -= uy * f
      b.vx += ux * f
      b.vy += uy * f
    }
  }
  for (const l of ls) {
    const a = at.get(l.src_id)
    const b = at.get(l.dst_id)
    if (!a || !b) continue
    const dx = b.x - a.x
    const dy = b.y - a.y
    const d = Math.max(Math.hypot(dx, dy), 1)
    const f = (d - SPRING_LEN) * SPRING_K * alpha
    const ux = dx / d
    const uy = dy / d
    a.vx += ux * f
    a.vy += uy * f
    b.vx -= ux * f
    b.vy -= uy * f
  }
  for (const n of ns) {
    // 节点越多向心引力越强：否则 100+ 节点的图会一路铺到画布外，打开先看到一片空白
    const grav = GRAVITY * Math.max(1, ns.length / 45)
    n.vx += (cx - n.x) * grav * alpha
    n.vy += (cy - n.y) * grav * alpha
    if (n.pinned) {
      n.vx = 0
      n.vy = 0
      continue
    }
    n.vx *= DAMP
    n.vy *= DAMP
    // 速度上限：斥力偶尔会叠出很大的瞬时值，限住之后图只是慢一点，不会飞
    n.vx = Math.max(-24, Math.min(24, n.vx))
    n.vy = Math.max(-24, Math.min(24, n.vy))
    n.x += n.vx
    n.y += n.vy
  }
  alpha = Math.max(0, alpha - 0.011)
}

function commit() {
  for (const n of nodes.value) {
    posCache.set(n.id, { x: n.x, y: n.y, pinned: n.pinned })
  }
}

function relayout() {
  layout(true)
}

function toggleKind(k) {
  const next = new Set(hiddenKinds.value)
  if (next.has(k)) next.delete(k)
  else next.add(k)
  hiddenKinds.value = next
  layout(false)
  // 得在重建之后判：nodes 还没换的时候就清选择，会清错
  if (selectedId.value && !nodes.value.some((n) => n.id === selectedId.value)) {
    selectedId.value = ''
  }
}

// ==================== 交互 ====================

function toWorld(evt) {
  const rect = svgEl.value?.getBoundingClientRect()
  if (!rect) return { x: 0, y: 0 }
  return {
    x: (evt.clientX - rect.left - view.value.tx) / view.value.k,
    y: (evt.clientY - rect.top - view.value.ty) / view.value.k,
  }
}

let drag = null

function onNodeDown(n, evt) {
  drag = { type: 'node', node: n, ...toWorld(evt) }
  drag.dx = n.x - drag.x
  drag.dy = n.y - drag.y
  n.pinned = true
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp, { once: true })
}

function onStageDown(evt) {
  if (evt.target.closest('.node')) return
  drag = { type: 'pan', sx: evt.clientX, sy: evt.clientY, tx: view.value.tx, ty: view.value.ty }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp, { once: true })
}

function onMove(evt) {
  if (!drag) return
  if (drag.type === 'node') {
    const p = toWorld(evt)
    drag.node.x = p.x + drag.dx
    drag.node.y = p.y + drag.dy
    if (alpha < 0.12) {
      alpha = 0.12
      if (!prefersReduced() && !raf) raf = requestAnimationFrame(frame)
    }
    commit()
  } else {
    view.value = {
      ...view.value,
      tx: drag.tx + (evt.clientX - drag.sx),
      ty: drag.ty + (evt.clientY - drag.sy),
    }
  }
}

function onUp() {
  drag = null
  window.removeEventListener('pointermove', onMove)
}

function onWheel(evt) {
  const rect = svgEl.value?.getBoundingClientRect()
  if (!rect) return
  const k = Math.min(3, Math.max(0.35, view.value.k * Math.exp(-evt.deltaY * 0.0016)))
  const px = evt.clientX - rect.left
  const py = evt.clientY - rect.top
  // 以指针为锚点缩放，否则一滚轮图就跑了
  view.value = {
    k,
    tx: px - ((px - view.value.tx) / view.value.k) * k,
    ty: py - ((py - view.value.ty) / view.value.k) * k,
  }
}

function close() {
  emit('update:open', false)
}

function onKeydown(e) {
  if (e.key === 'Escape') close()
}

watch(
  () => props.open,
  (v) => {
    if (v) {
      layout(false)
      window.addEventListener('keydown', onKeydown)
    } else {
      stop()
      onUp()
      window.removeEventListener('keydown', onKeydown)
    }
  },
  // post：刚打开时 svg 还没渲染出来，measure() 量不到尺寸，布局会围着一个假中心挤成一团
  { flush: 'post' }
)
watch(() => props.graph, () => { if (props.open) layout(false) })
onUnmounted(() => {
  stop()
  onUp()
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 120;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: var(--overlay);
}
.dlg {
  width: min(1180px, 100%);
  height: min(860px, 92vh);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  box-shadow: 0 32px 80px -32px rgba(var(--shade-rgb), 0.9);
  overflow: hidden;
}
.dlg-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px 12px;
  border-bottom: 1px solid var(--line-soft);
}
.head-txt {
  flex: 1;
  min-width: 0;
}
.dlg-head h2 {
  margin: 0 0 4px;
  font-family: theme('fontFamily.serif');
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}
.dlg-sub {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-faint);
}
.head-acts {
  display: flex;
  align-items: center;
  gap: 8px;
}
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.icon-btn:hover {
  color: var(--text);
  border-color: var(--line-strong);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--line-soft);
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--panel-2);
  color: var(--text-muted);
  font-size: 11.5px;
  cursor: pointer;
  transition: opacity 0.18s var(--ease), color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.chip:hover {
  border-color: var(--line-strong);
  color: var(--text);
}
.chip.off {
  opacity: 0.4;
}
.chip em {
  font-family: theme('fontFamily.mono');
  font-style: normal;
  font-size: 10px;
  color: var(--text-faint);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.body {
  flex: 1;
  display: flex;
  min-height: 0;
}
.stage {
  flex: 1;
  min-width: 0;
  background:
    radial-gradient(circle at 30% 20%, rgba(var(--info-rgb), 0.05), transparent 60%),
    var(--bg);
  touch-action: none;
  cursor: grab;
}
.stage:active {
  cursor: grabbing;
}
.link {
  stroke: var(--line-strong);
  stroke-width: 1.1;
  opacity: 0.75;
}
.link.dead {
  stroke-dasharray: 4 4;
  stroke: var(--dead-link);
  opacity: 0.8;
}
.link.hi {
  stroke: var(--run);
  opacity: 0.95;
}
.link-label {
  fill: var(--run);
  font-size: 10px;
  text-anchor: middle;
  paint-order: stroke;
  stroke: var(--bg);
  stroke-width: 3;
  pointer-events: none;
}
.node {
  cursor: pointer;
}
.node circle {
  stroke: rgba(var(--over), 0.28);
  stroke-width: 1;
  fill-opacity: 0.86;
  transition: fill-opacity 0.18s var(--ease), stroke 0.18s var(--ease);
}
.node circle.gone {
  fill-opacity: 0.22;
  stroke: var(--danger);
  stroke-dasharray: 3 2;
}
.node:hover circle {
  fill-opacity: 1;
  stroke: var(--text);
}
.node.sel circle {
  stroke: var(--run);
  stroke-width: 2;
  fill-opacity: 1;
}
.node.dim {
  opacity: 0.26;
}
.label {
  fill: var(--text-muted);
  font-size: 10.5px;
  text-anchor: middle;
  paint-order: stroke;
  stroke: var(--bg);
  stroke-width: 3;
  pointer-events: none;
}
.node.sel .label {
  fill: var(--text);
}
.blank {
  fill: var(--text-faint);
  font-size: 12px;
  text-anchor: middle;
}
.detail {
  width: 262px;
  flex-shrink: 0;
  padding: 14px;
  border-left: 1px solid var(--line-soft);
  background: var(--panel-2);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.d-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.d-name {
  font-family: theme('fontFamily.serif');
  font-size: 15px;
  color: var(--text);
}
.d-kind {
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
  color: var(--accent-ink);
}
.d-status {
  margin: 0;
  font-size: 11.5px;
  color: var(--danger);
}
.d-status em {
  font-style: normal;
  color: var(--text-faint);
  margin-left: 4px;
}
.d-meta {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.d-meta > div {
  display: flex;
  gap: 8px;
  font-size: 11.5px;
}
.d-meta dt {
  width: 62px;
  flex-shrink: 0;
  color: var(--text-faint);
}
.d-meta dd {
  margin: 0;
  color: var(--text-muted);
  word-break: break-word;
}
.d-card {
  margin: 0;
  padding: 8px 9px;
  border-left: 2px solid rgba(var(--run-rgb), 0.45);
  border-radius: 0 6px 6px 0;
  background: rgba(var(--run-rgb), 0.05);
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-muted);
}
.d-rels {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.d-rels li {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.r-other {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.r-type {
  padding: 0 4px;
  border-radius: 3px;
  background: rgba(var(--run-rgb), 0.1);
  color: var(--run-fg);
  font-size: 10px;
  flex-shrink: 0;
}
.r-dead {
  color: var(--danger);
  font-size: 10px;
}
.r-since {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  color: var(--text-faint);
}
.d-tip {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-muted);
}
.d-tip.faint,
.d-card.faint {
  color: var(--text-faint);
}
.legend {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.legend li {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.legend em {
  margin-left: auto;
  font-style: normal;
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  color: var(--text-faint);
}
</style>
