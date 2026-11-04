<template>
  <div class="craft">
    <!-- 发起条：选模型、选层级、要不要推翻旧卡。三件事一屏说清，不弹窗 -->
    <div class="blk">
      <div class="blk-head">
        <span class="blk-title">手艺拆解</span>
        <button
          class="btn btn-primary btn-sm"
          :disabled="starting || busy || !levels.length"
          @click="start"
        >{{ starting ? '入队中…' : '开始拆解' }}</button>
      </div>
      <p class="hint">
        逐章拆场景/冲突/信息差，卷汇总弧线与伏笔，全书提幕结构与可借鉴点。
        产物是结构卡片：可以拷进别的书的素材桶，或抽成模板，不绑定「仿写」这一种用法。
      </p>

      <label class="fl">
        <span>模型</span>
        <select
          class="field"
          :value="store.genModel"
          @change="store.setGenModel($event.target.value)"
        >
          <option value="">自动</option>
          <optgroup v-for="g in store.modelGroups" :key="g.key" :label="g.name">
            <option v-for="m in g.models" :key="m.id" :value="m.id">{{ m.label }}</option>
          </optgroup>
        </select>
      </label>

      <label class="fl">
        <span>范围</span>
        <select v-model="scopeNodeId" class="field scope">
          <option value="">整本书（所有卷与章）</option>
          <option v-for="c in scopeOptions" :key="c.id" :value="c.id">
            {{ c.title || '未命名卷' }}
          </option>
        </select>
      </label>
      <p v-if="scopeInfo" class="hint">
        只拆《{{ scopeInfo.title }}》这一支：{{ scopeInfo.chapters }} 章 + 本卷汇总。
        百万字全书要几百次调用、挂机几小时，先拆一卷看样本再铺全书更稳；
        全书级等各卷拆完再单独点（缺卷的架构分析不算结论）。
      </p>

      <div class="level-row">
        <label v-for="lv in LEVELS" :key="lv.id" class="chk">
          <input
            v-model="levels"
            type="checkbox"
            :value="lv.id"
            :disabled="lv.id === 'book' && !!scopeNodeId"
          />
          <span>{{ lv.label }}</span>
        </label>
      </div>
      <label class="chk">
        <input v-model="force" type="checkbox" />
        <span>强制重拆（不勾：正文没改过的章跳过，省钱）</span>
      </label>

      <p v-if="startNote" class="msg" :class="{ 'is-bad': startBad }">{{ startNote }}</p>
      <p v-if="lastRun" class="hint run-line">
        已入队 {{ lastRun.queued }} 条<template v-if="lastRun.deduped">（队列里已有 {{ lastRun.deduped }} 条同样任务，没重复排）</template>
        · 可拆章 {{ lastRun.chapters }}
        · 预计投喂 {{ fmt(lastRun.estimate && lastRun.estimate.prompt_chars_total) }} 字
        <template v-if="lastRun.estimate && lastRun.estimate.priced">
          · 预估 ≈ {{ lastRun.estimate.est_cost }}
        </template>
      </p>
      <p v-if="lastRun && lastRun.note" class="hint">{{ lastRun.note }}</p>

      <!-- 402：预估超预算。拦的是手滑，不拦清醒的决定——勾选后原样重发 -->
      <div v-if="budgetBlock" class="budget">
        <p class="msg is-bad">{{ budgetBlock }}</p>
        <label class="chk">
          <input v-model="ignoreBudget" type="checkbox" />
          <span>我已看过预估，忽略预算上限强行入队</span>
        </label>
        <button class="btn btn-sm" :disabled="starting" @click="start">再来一次</button>
      </div>
    </div>

    <!-- 进度：入队后关页面也接着跑，这里只负责把队列状态摆出来 -->
    <div class="blk">
      <div class="blk-head">
        <span class="blk-title">进度</span>
        <button class="btn btn-sm" :disabled="loading" @click="refresh">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
      <p class="hint">
        已拆 {{ data.analysed_chapters }}/{{ data.total_chapters }} 章
        <template v-if="progressTotal"> · 任务 {{ progressTotal }} 条</template>
      </p>
      <div v-if="progressTotal" class="progress-row">
        <span v-for="(n, k) in data.progress" :key="k" class="pill" :class="pillOf(k)">
          {{ STATUS_LABEL[k] || k }} {{ n }}
        </span>
      </div>
      <p v-else class="hint">
        还没有拆解任务。任务进队列后由后台执行，关掉页面也不影响。
      </p>
    </div>

    <!-- 卡片列表：后端已按 全书 → 卷 → 章 排好，这里不再重排 -->
    <div v-if="data.analyses.length" class="blk">
      <div class="blk-head">
        <span class="blk-title">结构卡片</span>
        <span class="hint inline">{{ data.analyses.length }} 张</span>
      </div>

      <div class="pick-row">
        <label class="chk">
          <input type="checkbox" :checked="allPicked" @change="toggleAll($event.target.checked)" />
          <span>全选</span>
        </label>
        <select v-model="targetWorkId" class="field target" aria-label="拷贝目标作品">
          <option v-for="w in workOptions" :key="w.id" :value="w.id">
            {{ w.title || '未命名' }}{{ w.id === store.activeWorkId ? '（本书）' : '' }}
          </option>
        </select>
        <button class="btn btn-sm" :disabled="applying || !picked.length" @click="applyTo">
          {{ applying ? '拷贝中…' : `拷到书（${picked.length}）` }}
        </button>
      </div>
      <p class="hint">
        拷贝会在目标书的素材桶里生成一条【结构】素材并立即建索引：之后写那本书时
        按本节检索得到的是具体结构事实，不是一句「参考某书」。
      </p>
      <p v-if="applyNote" class="msg" :class="{ 'is-bad': applyBad }">{{ applyNote }}</p>

      <!-- 书卡默认展开：它只有一张且是整本书的结论，「抽成模板」藏在折叠里
           没人会发现；章卡可能上百张，必须折叠保密度 -->
      <details
        v-for="c in data.analyses"
        :key="c.id"
        class="card-item"
        :class="'lv-' + c.level"
        :open="c.level === 'book'"
      >
        <summary>
          <input
            type="checkbox"
            :checked="picked.includes(c.id)"
            aria-label="选中这张卡"
            @click.stop
            @change="togglePick(c.id)"
          />
          <span class="lv-badge">{{ LEVEL_LABEL[c.level] || c.level }}</span>
          <span class="card-name">{{ c.title || '未命名' }}</span>
          <span class="card-meta">{{ cardMeta(c) }}</span>
          <span v-if="c.note" class="card-note" :title="c.note">{{ c.note }}</span>
        </summary>
        <pre class="card-text">{{ c.text }}</pre>

        <!-- 全书卡多一条出路：幕结构直接沉淀成模板，进模板库可套用 -->
        <div v-if="c.level === 'book'" class="card-act">
          <template v-if="tplFor === c.id">
            <input
              v-model="tplName"
              class="field tpl-name"
              placeholder="模板名（留空用「书名 · 结构拆解」）"
              @keyup.enter="confirmTpl"
            />
            <button class="btn btn-sm" :disabled="tplBusy" @click="confirmTpl">
              {{ tplBusy ? '创建中…' : '建模板' }}
            </button>
            <button class="btn btn-sm" @click="tplFor = ''">取消</button>
          </template>
          <button v-else class="btn btn-sm" @click="openTpl(c.id)">抽成模板</button>
          <span v-if="tplNote" class="hint inline">{{ tplNote }}</span>
        </div>
      </details>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { apiFetch } from '../utils/api'
import { useStudioStore } from '../stores/studio'

const store = useStudioStore()

const LEVELS = [
  { id: 'chapter', label: '逐章' },
  { id: 'volume', label: '逐卷' },
  { id: 'book', label: '全书' },
]
const LEVEL_LABEL = { chapter: '章', volume: '卷', book: '全书' }
const STATUS_LABEL = { queued: '排队', running: '进行中', done: '完成', failed: '失败' }

const levels = ref(LEVELS.map((l) => l.id))
const force = ref(false)
const ignoreBudget = ref(false)
// 空 = 整本书；给定 = 只拆一棵子树（卷）
const scopeNodeId = ref('')

const starting = ref(false)
const startNote = ref('')
const startBad = ref(false)
const budgetBlock = ref('')
const lastRun = ref(null)

const loading = ref(false)
const data = ref({ analyses: [], progress: {}, total_chapters: 0, analysed_chapters: 0 })

const picked = ref([])
const targetWorkId = ref('')
const workOptions = ref([])
const applying = ref(false)
const applyNote = ref('')
const applyBad = ref(false)

const tplFor = ref('')
const tplName = ref('')
const tplBusy = ref(false)
const tplNote = ref('')

const busy = computed(() => (data.value.progress?.running || 0) > 0)
// 可选范围只有容器（卷）；单层书没有卷时下拉里只剩「整本书」一个选项
const scopeOptions = computed(() =>
  (store.tree || []).filter((n) => n.ordinal == null))
const scopeInfo = computed(() => {
  const id = scopeNodeId.value
  if (!id) return null
  const sc = (store.tree || []).find((n) => n.id === id)
  if (!sc) return null
  const prefix = (sc.path || '') + '.'
  const chapters = (store.tree || []).filter(
    (n) => n.ordinal != null && (n.path || '').startsWith(prefix))
  return { title: sc.title || '未命名卷', chapters: chapters.length }
})
const progressTotal = computed(() =>
  Object.values(data.value.progress || {}).reduce((a, b) => a + b, 0))
const allPicked = computed(() =>
  data.value.analyses.length > 0 && picked.value.length === data.value.analyses.length)

function fmt(n) {
  return (n || 0).toLocaleString()
}

function clip(text, n) {
  const s = String(text || '')
  return s.length > n ? s.slice(0, n) + '…' : s
}

/** FastAPI 的 detail 三种形态：字符串 / 校验数组 / 闸门对象，都摊平成一句人话 */
function detailText(detail) {
  if (!detail) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((d) => d?.msg || JSON.stringify(d)).join('；')
  }
  if (typeof detail.message === 'string') return detail.message
  return ''
}

function pillOf(status) {
  if (status === 'done') return 'pill--on'
  if (status === 'running' || status === 'failed') return 'pill--warn'
  return 'pill--off'
}

/** 卡片摘要：决定「这摞卡值不值得拷」的信息，比看到全文更快 */
function cardMeta(c) {
  const d = c.data || {}
  if (c.level === 'chapter') {
    const n = (d.scenes || []).length
    const hook = d.hook ? ` · 钩子：${clip(d.hook, 28)}` : ''
    return `${n} 个场景${hook}`
  }
  if (c.level === 'volume') {
    const arc = d.arc ? `弧线：${clip(d.arc, 36)}` : ''
    const f = (d.foreshadows || []).length
    return arc + (f ? ` · 伏笔 ${f} 对` : '')
  }
  if (c.level === 'book') {
    const acts = (d.acts || []).length
    return (d.structure_type || '') + (acts ? ` · ${acts} 幕` : '')
  }
  return ''
}

async function refresh() {
  if (loading.value || !store.activeWorkId) return
  loading.value = true
  try {
    const r = await apiFetch(`/studio/works/${store.activeWorkId}/craft`)
    if (!r.ok) return
    const j = await r.json()
    data.value = {
      analyses: j.analyses || [],
      progress: j.progress || {},
      total_chapters: j.total_chapters || 0,
      analysed_chapters: j.analysed_chapters || 0,
    }
    // 卡片换了新列表，勾选里可能留着已不存在的 id：清掉，否则「拷到书（3）」实际只发得出 2 条
    const ids = new Set(data.value.analyses.map((c) => c.id))
    picked.value = picked.value.filter((id) => ids.has(id))
  } finally {
    loading.value = false
  }
}

async function loadWorkOptions() {
  const r = await apiFetch('/studio/works')
  if (!r.ok) {
    workOptions.value = store.work ? [store.work] : []
    return
  }
  const list = await r.json()
  workOptions.value = Array.isArray(list) ? list : []
  if (!workOptions.value.some((w) => w.id === store.activeWorkId) && store.work) {
    workOptions.value.unshift(store.work)
  }
}

async function start() {
  if (starting.value) return
  starting.value = true
  startNote.value = ''
  startBad.value = false
  budgetBlock.value = ''
  try {
    const r = await apiFetch(`/studio/works/${store.activeWorkId}/craft`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: store.genModel || '',
        levels: levels.value,
        force: force.value,
        ignore_budget: ignoreBudget.value,
        scope_node_id: scopeNodeId.value,
      }),
    })
    const j = await r.json().catch(() => ({}))
    if (r.status === 402) {
      budgetBlock.value = detailText(j.detail) || '预估超预算上限'
      return
    }
    if (!r.ok) {
      startNote.value = detailText(j.detail) || `入队失败（HTTP ${r.status}）`
      startBad.value = true
      return
    }
    lastRun.value = j
    ignoreBudget.value = false
    await refresh()
  } catch {
    startNote.value = '入队请求没发出去：检查服务是否在跑'
    startBad.value = true
  } finally {
    starting.value = false
  }
}

function togglePick(id) {
  picked.value = picked.value.includes(id)
    ? picked.value.filter((x) => x !== id)
    : [...picked.value, id]
}

function toggleAll(on) {
  picked.value = on ? data.value.analyses.map((c) => c.id) : []
}

async function applyTo() {
  if (applying.value || !picked.value.length || !targetWorkId.value) return
  applying.value = true
  applyNote.value = ''
  applyBad.value = false
  try {
    const r = await apiFetch(`/studio/works/${store.activeWorkId}/craft/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_work_id: targetWorkId.value,
        analysis_ids: picked.value,
      }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) {
      applyNote.value = detailText(j.detail) || `拷贝失败（HTTP ${r.status}）`
      applyBad.value = true
      return
    }
    const failed = j.failed || []
    applyNote.value = `已拷 ${j.ok} 条进《${j.target_title || '目标作品'}》的素材桶` +
      (failed.length ? `；${failed.length} 条没成功：${failed[0].error || '未知原因'}` : '')
    applyBad.value = failed.length > 0
    picked.value = []
    // 拷进本书时右栏「素材」计数要跟着变，否则看着像没拷进去
    if (targetWorkId.value === store.activeWorkId) await store.loadSources()
  } catch {
    applyNote.value = '拷贝请求没发出去：检查服务是否在跑'
    applyBad.value = true
  } finally {
    applying.value = false
  }
}

function openTpl(id) {
  tplFor.value = id
  tplName.value = ''
  tplNote.value = ''
}

async function confirmTpl() {
  if (tplBusy.value) return
  tplBusy.value = true
  tplNote.value = ''
  try {
    const r = await apiFetch(`/studio/templates/from-craft/${store.activeWorkId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: tplName.value.trim() }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) {
      tplNote.value = detailText(j.detail) || `建模板失败（HTTP ${r.status}）`
      return
    }
    tplFor.value = ''
    tplNote.value = `已建成模板「${j.name || ''}」：去「模板」页可以套用到新作品`
  } catch {
    tplNote.value = '请求没发出去：检查服务是否在跑'
  } finally {
    tplBusy.value = false
  }
}

// 队列里还有活就自动刷：拆一章动辄十几秒到几分钟，手点刷新跟不上
let pollTimer = 0
function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(() => {
    if (!loading.value) refresh()
  }, 4000)
}
function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = 0
  }
}
watch(busy, (v) => (v ? startPoll() : stopPoll()), { immediate: true })

// 选了范围就不该再勾全书：后端会默默拿掉，前端先把按钮灰掉、把值清掉，
// 否则用户看着勾上了却没出书卡，只会以为拆解坏了。
watch(scopeNodeId, (id) => {
  if (id && levels.value.includes('book')) {
    levels.value = levels.value.filter((x) => x !== 'book')
  }
})

// 切作品：卡片、勾选、默认目标书全部换成本书视角
watch(
  () => store.activeWorkId,
  async (id) => {
    if (!id) return
    data.value = { analyses: [], progress: {}, total_chapters: 0, analysed_chapters: 0 }
    picked.value = []
    lastRun.value = null
    startNote.value = ''
    budgetBlock.value = ''
    applyNote.value = ''
    tplFor.value = ''
    tplNote.value = ''
    scopeNodeId.value = ''
    targetWorkId.value = id
    await Promise.all([refresh(), loadWorkOptions()])
  },
)

onMounted(async () => {
  targetWorkId.value = store.activeWorkId
  await Promise.all([refresh(), loadWorkOptions()])
})

onBeforeUnmount(stopPoll)
</script>

<style scoped>
/* 与 StudioPanel 的分区同一套呼吸：块间距、标题层、提示行颜色都跟右栏对齐 */
.craft {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.blk {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line-soft);
}
.blk:last-child {
  border-bottom: none;
  padding-bottom: 0;
}
.blk-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.blk-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
}
.hint {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-faint);
}
.hint.inline {
  display: inline;
}
.empty {
  margin: 0;
  font-size: 12px;
  color: var(--text-faint);
}
.fl {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11.5px;
  color: var(--text-faint);
}
.level-row {
  display: flex;
  gap: 14px;
}
.chk {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text);
  cursor: pointer;
}
.msg {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
}
.msg.is-bad {
  color: var(--danger);
}
.budget {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.run-line {
  color: var(--text-muted);
}
.progress-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.pick-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.target {
  flex: 1;
  min-width: 0;
  height: 28px;
  font-size: 12px;
}
.card-item {
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  padding: 8px 10px;
  background: rgba(var(--over), 0.02);
}
.card-item summary {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  list-style: none;
}
.card-item summary::-webkit-details-marker {
  display: none;
}
.lv-badge {
  flex: none;
  padding: 0 6px;
  border: 1px solid var(--line);
  border-radius: 4px;
  font-size: 10.5px;
  line-height: 17px;
  color: var(--text-faint);
}
.card-item.lv-book .lv-badge {
  border-color: rgba(var(--run-rgb), 0.4);
  color: var(--run-fg);
}
.card-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card-meta {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card-note {
  flex: none;
  max-width: 40%;
  font-size: 10.5px;
  color: var(--warn-fg);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card-text {
  margin: 8px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(var(--over), 0.04);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow-y: auto;
}
.card-act {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.tpl-name {
  flex: 1;
  min-width: 160px;
  height: 28px;
  font-size: 12px;
}
</style>
