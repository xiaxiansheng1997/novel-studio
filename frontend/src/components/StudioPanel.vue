<template>
  <aside class="panel">
    <div class="tabs" role="tablist">
      <button
        v-for="t in tabs"
        :key="t.id"
        class="tab"
        :class="{ 'is-active': tab === t.id }"
        role="tab"
        :aria-selected="tab === t.id"
        @click="tab = t.id"
      >
        {{ t.label }}
        <span v-if="t.count" class="tab-count">{{ t.count }}</span>
      </button>
    </div>

    <div class="panel-body">
      <!-- 素材 -->
      <section v-if="tab === 'sources'" class="section">
        <p class="hint">生成时按相关度取前几条注入；单条建议不超过 2 万字。</p>
        <div v-for="s in store.sources" :key="s.id" class="item">
          <div class="item-main">
            <span class="item-title">{{ s.title || '未命名素材' }}</span>
            <span class="item-meta">{{ s.published_at || '无日期' }} · {{ s.chars }} 字</span>
          </div>
          <button class="mini-btn" title="删除" @click="store.deleteSource(s.id)">
            <AppIcon name="trash" :size="13" />
          </button>
        </div>
        <p v-if="!store.sources.length" class="empty">
          还没有素材。时政/历史类必须导入资料，否则模型会编造事实。
        </p>

        <details class="add-box">
          <summary>添加素材</summary>
          <input v-model="src.title" class="field" placeholder="标题（如：官方 changelog）" />
          <input v-model="src.url" class="field" placeholder="来源链接（可选）" />
          <input v-model="src.published_at" class="field" placeholder="资料日期 YYYY-MM（可选）" />
          <textarea v-model="src.content" class="field" rows="5" placeholder="粘贴正文…" />
          <button class="btn btn-primary btn-sm" :disabled="!src.content.trim() || busy" @click="submitSource">
            导入
          </button>
        </details>
      </section>

      <!-- 事实 -->
      <section v-else-if="tab === 'facts'" class="section">
        <p class="hint">把素材压成「一句可引用的话」，生成时优先使用。</p>
        <div v-for="f in store.facts" :key="f.id" class="item">
          <div class="item-main">
            <span class="item-title">{{ f.statement }}</span>
            <span class="item-meta">
              {{ f.source_title || '无来源' }}
              <template v-if="f.valid_until"> · 有效至 {{ f.valid_until }}</template>
            </span>
          </div>
          <button class="mini-btn" title="删除" @click="store.deleteFact(f.id)">
            <AppIcon name="trash" :size="13" />
          </button>
        </div>
        <p v-if="!store.facts.length" class="empty">还没有事实卡。</p>

        <details class="add-box">
          <summary>添加事实</summary>
          <textarea v-model="fact.statement" class="field" rows="2" placeholder="一句话陈述…" />
          <input v-model="fact.valid_until" class="field" placeholder="有效期 YYYY-MM（可选）" />
          <button class="btn btn-primary btn-sm" :disabled="!fact.statement.trim() || busy" @click="submitFact">
            添加
          </button>
        </details>
      </section>

      <!-- 术语 -->
      <section v-else-if="tab === 'terms'" class="section">
        <p class="hint">技术类必用：统一写法，禁用说法会在检查时报出。</p>
        <div v-for="t in store.terms" :key="t.id" class="item">
          <div class="item-main">
            <span class="item-title">{{ t.term }}</span>
            <span class="item-meta">写作「{{ t.preferred || t.term }}」</span>
          </div>
          <button class="mini-btn" title="删除" @click="store.deleteTerm(t.id)">
            <AppIcon name="trash" :size="13" />
          </button>
        </div>
        <p v-if="!store.terms.length" class="empty">还没有术语。</p>

        <details class="add-box">
          <summary>添加术语</summary>
          <input v-model="term.term" class="field" placeholder="术语（如：云原生）" />
          <input v-model="term.preferred" class="field" placeholder="统一写作（默认同术语）" />
          <input v-model="term.forbiddenText" class="field" placeholder="禁用说法，逗号分隔" />
          <button class="btn btn-primary btn-sm" :disabled="!term.term.trim() || busy" @click="submitTerm">
            添加
          </button>
        </details>
      </section>

      <!-- 上下文 -->
      <section v-else-if="tab === 'context'" class="section">
        <!-- 记忆状态：索引与摘要。没建索引时 S4/S5 是空的，先在这里看得到 -->
        <div class="mem">
          <div class="mem-head">
            <span class="mem-title">记忆</span>
            <span v-if="st" class="mem-stat">
              {{ st.chunks_body }} 块 · {{ st.nodes_indexed }}/{{ st.nodes_total }} 节
              · 摘要 {{ st.summaries }}
              <template v-if="staleCount"> · {{ staleCount }} 份已过期</template>
            </span>
          </div>
          <div class="mem-actions">
            <button class="btn btn-sm" :disabled="store.indexing || store.summarizing" @click="doIndex(false)">
              {{ store.indexing ? '建索引…' : '建索引' }}
            </button>
            <button class="btn btn-sm" :disabled="store.indexing || store.summarizing" @click="doIndex(true)">
              全量重建
            </button>
            <button class="btn btn-sm" :disabled="store.indexing || store.summarizing" @click="doSummarize">
              {{ store.summarizing ? '摘要中…' : (staleCount ? '重算过期摘要' : '生成摘要') }}
            </button>
          </div>
          <p v-if="store.memoryNote" class="hint">{{ store.memoryNote }}</p>
          <p v-else-if="staleCount" class="hint">
            这几节的正文改过但摘要还是旧的：生成时不会注入它们（宁缺勿假），点上面重算。
          </p>
          <p v-else-if="st && st.chunks_body === 0" class="hint">
            还没建索引：不建索引，S5 检索证据与 S4 最近摘要都不会进上下文。
          </p>
        </div>

        <div class="ctx-head">
          <span class="hint inline">装配预算（调 prompt 先看这里）</span>
          <button class="btn btn-sm" :disabled="loadingCtx" @click="refreshContext">
            {{ loadingCtx ? '装配中…' : '刷新' }}
          </button>
        </div>

        <template v-if="ctx">
          <div v-for="b in ctx.buckets" :key="b.id" class="bucket">
            <div class="bucket-top">
              <span class="bucket-name">{{ b.id }} {{ b.label }}</span>
              <span class="bucket-num">{{ b.chars }}/{{ b.budget }}</span>
            </div>
            <div class="bar">
              <div class="bar-fill" :class="{ over: b.chars >= b.budget }" :style="{ width: barWidth(b) }"></div>
            </div>
          </div>
          <p class="ctx-total">总计 {{ ctx.total_chars }} 字符（不含目标字数）</p>
          <p v-for="(w, i) in ctx.warnings" :key="i" class="warn-line">{{ w }}</p>

          <template v-if="ctx.evidence && ctx.evidence.length">
            <p class="ctx-total">检索证据 {{ ctx.evidence.length }} 条</p>
            <div v-for="e in ctx.evidence" :key="e.chunk_id" class="ev">
              <div class="ev-head">
                <span class="ev-title">{{ e.title }}</span>
                <span class="ev-meta">
                  {{ e.ordinal ? '第' + e.ordinal + '节' : '素材' }} · {{ e.score }}
                </span>
              </div>
              <p class="ev-text">{{ e.text }}</p>
            </div>
          </template>

          <details class="raw">
            <summary>查看 system / user 原文</summary>
            <pre>{{ ctx.system }}</pre>
            <pre>{{ ctx.user }}</pre>
          </details>
        </template>
        <p v-else class="empty">点「刷新」装配一次，只算不调模型。</p>
      </section>

      <!-- 图谱（仅小说） -->
      <section v-else-if="tab === 'graph'" class="section">
        <div class="mem">
          <div class="mem-head">
            <span class="mem-title">设定图谱</span>
            <span v-if="store.graphData" class="mem-stat">
              {{ store.graphData.stats.entities }} 实体 ·
              {{ store.graphData.stats.relations }} 关系 ·
              {{ store.graphData.stats.foreshadows_open }} 伏笔
            </span>
          </div>
          <div class="mem-actions">
            <button class="btn btn-sm" :disabled="store.graphBusy || !store.activeNodeId" @click="doExtract({})">
              {{ store.graphBusy ? '抽取中…' : '抽本节' }}
            </button>
            <button class="btn btn-sm" :disabled="store.graphBusy" @click="doExtract({ all: true })">
              抽全书
            </button>
            <button class="btn btn-sm" :disabled="store.graphBusy || !store.activeNodeId" @click="doExtract({ all: false, force: true })">
              重抽
            </button>
            <button class="btn btn-sm" :disabled="!graphEntities.length" @click="graphOpen = true">
              图形视图
            </button>
          </div>
          <p v-if="store.graphNote" class="hint">{{ store.graphNote }}</p>
          <p v-else-if="store.graphData && !store.graphData.stats.entities" class="hint">
            还没有设定。先写几章定稿再抽取，S6 才能把人物状态带进上下文。
          </p>
        </div>

        <template v-if="store.graphData && store.graphData.stats.entities">
          <div v-for="kind in KIND_ORDER" :key="kind">
            <template v-if="graphByKind[kind] && graphByKind[kind].length">
              <p class="g-kind">{{ KIND_LABEL[kind] }}</p>
              <div v-for="e in graphByKind[kind]" :key="e.id" class="g-row">
                <span class="g-name">{{ e.name }}</span>
                <span v-if="e.status !== 'alive'" class="g-badge">{{ STATUS_LABEL[e.status] || e.status }}</span>
                <span v-if="e.card_id" class="g-card-tag">卡</span>
              </div>
            </template>
          </div>

          <template v-if="graphRelations.length">
            <p class="g-kind">关系</p>
            <div v-for="r in graphRelations" :key="r.id" class="g-rel">
              <span>{{ r.src_name }}</span>
              <span class="g-rt">{{ r.rel_type }}</span>
              <span>{{ r.dst_name }}</span>
            </div>
          </template>

          <template v-if="graphForeshadows.length">
            <p class="g-kind">未回收伏笔</p>
            <div v-for="f in graphForeshadows" :key="f.id" class="g-fs">
              <span class="g-fs-ord">第{{ f.planted_ordinal }}节</span>
              <span>{{ f.content }}</span>
            </div>
          </template>
        </template>

        <!-- 设定卡：作者写的东西必须比模型抽的更有分量 -->
        <details class="raw" :open="!graphCards.length">
          <summary>设定卡（{{ graphCards.length }}）</summary>
          <div v-for="c in graphCards" :key="c.id" class="item">
            <div class="item-main">
              <span class="item-title">{{ c.name }}</span>
              <span class="item-meta">{{ KIND_LABEL[c.kind] || c.kind }}</span>
            </div>
            <p v-if="c.summary" class="hint">{{ c.summary }}</p>
            <button class="btn btn-sm" @click="store.deleteCard(c.id)">删</button>
          </div>
          <div class="fl">
            <select v-model="cardForm.kind" class="field">
              <option v-for="k in KIND_ORDER" :key="k" :value="k">{{ KIND_LABEL[k] }}</option>
            </select>
            <input v-model="cardForm.name" class="field" placeholder="名字（必须与正文一致）" />
            <input v-model="cardForm.summary" class="field" placeholder="一句话设定" />
            <select v-model="cardForm.status" class="field">
              <option value="">状态不变</option>
              <option value="dead">已死亡</option>
              <option value="destroyed">已毁</option>
              <option value="missing">下落不明</option>
            </select>
            <button class="btn btn-sm" @click="submitCard">加卡</button>
          </div>
          <p class="hint">
            设定卡是权威：抽取时会作为硬约束注入，状态从「第 N 节」起生效。
          </p>
        </details>
      </section>

      <!-- 告警 -->
      <section v-else class="section">
        <div class="ctx-head">
          <span class="hint inline">
            <template v-if="store.issueSummary">
              未处理 {{ store.issueSummary.open }} 条（错误 {{ store.issueSummary.errors }}）
            </template>
            <template v-else>一致性检查</template>
          </span>
          <button class="btn btn-sm" :disabled="store.checking" @click="doCheck(false)">
            {{ store.checking ? '检查中…' : '检查本节' }}
          </button>
        </div>
        <p v-if="!store.issues.length" class="empty">当前节点没有告警。</p>
        <div v-for="i in hardIssues" :key="i.id" class="item issue">
          <div class="item-main">
            <span class="item-title">{{ i.message }}</span>
            <span class="item-meta">{{ ruleLabel(i.rule) }} · 第 {{ i.location || '?' }} 节</span>
          </div>
          <p v-if="i.evidence" class="issue-ev">{{ i.evidence }}</p>
          <div class="issue-act">
            <button v-if="i.evidence" class="btn btn-sm" @click="emit('locate', i.evidence)">定位</button>
            <button class="btn btn-sm" @click="store.resolveIssue(i.id)">忽略</button>
          </div>
        </div>

        <!-- 软规则：字面模式猜出来的，只提醒不参与扣分，所以单独一组，别和硬错误混在一起 -->
        <template v-if="softIssues.length">
          <p class="g-kind">待确认（不扣分）</p>
          <div v-for="i in softIssues" :key="i.id" class="item issue soft">
            <div class="item-main">
              <span class="item-title">{{ i.message }}</span>
              <span class="item-meta">{{ ruleLabel(i.rule) }}</span>
            </div>
            <p v-if="i.evidence" class="issue-ev">{{ i.evidence }}</p>
            <div class="issue-act">
              <button v-if="i.evidence" class="btn btn-sm" @click="emit('locate', i.evidence)">定位</button>
              <button class="btn btn-sm" @click="store.resolveIssue(i.id)">就是这样</button>
            </div>
          </div>
        </template>
        <p class="hint">
          告警不阻塞保存，但定稿前硬告警应该清零。小说 5 条 + 文档 4 条硬规则、
          以及称呼漂移等 4 条软规则全部是本地比对，不花调用；软规则只出「待确认」，不拖低分数。
        </p>
      </section>
    </div>

    <GraphMap
      v-model:open="graphOpen"
      :graph="store.graphData"
      @locate="(t) => emit('locate', t)"
    />
  </aside>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import GraphMap from './GraphMap.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  task: { type: Object, default: () => ({ task_type: 'article_draft', instruction: '' }) },
})
const emit = defineEmits(['locate'])

const store = useStudioStore()
const tab = ref('sources')
const busy = ref(false)
const loadingCtx = ref(false)
const ctx = ref(null)
const st = computed(() => store.indexStatus)
// 过期摘要 = 正文改过但摘要还是旧的（节级按内容指纹判）+ 由它拼出来的卷/全书摘要。
// 两者分开计数是为了说清「重算」要花多少调用：节摘要要重跑，上游只要重拼。
const staleCount = computed(
  () => (st.value?.stale_summaries || 0) + (st.value?.stale_upstream || 0)
)

const src = ref({ title: '', url: '', published_at: '', content: '' })
const fact = ref({ statement: '', valid_until: '' })
const term = ref({ term: '', preferred: '', forbiddenText: '' })

// ---- 图谱 ----
const KIND_LABEL = {
  character: '人物', location: '地点', item: '物品',
  faction: '势力', rule: '规则', event: '事件', foreshadow: '伏笔',
}
const STATUS_LABEL = { dead: '已死', destroyed: '已毁', missing: '失踪' }
const KIND_ORDER = ['character', 'location', 'item', 'faction', 'rule']

const graphEntities = computed(() => store.graphData?.entities || [])
const graphByKind = computed(() => {
  const out = {}
  for (const e of graphEntities.value) {
    ;(out[e.kind] ||= []).push(e)
  }
  return out
})
const graphRelations = computed(() => store.graphData?.relations || [])
const graphForeshadows = computed(() =>
  (store.graphData?.foreshadows || []).filter((f) => f.status === 'open')
)
const graphCards = computed(() => store.graphData?.cards || [])
const cardForm = ref({ kind: 'character', name: '', summary: '', status: '' })
const graphOpen = ref(false)

// 告警直接显示 dead_character 这种内部名，作者看不出轻重；硬告警与待确认分开展示
const RULE_LABEL = {
  dead_character: '死人复活', item_conflict: '归属冲突', time_regression: '时间倒退',
  location_destroyed: '已毁地点', foreshadow_overdue: '伏笔超期',
  fact_stale: '事实过期', citation_missing: '缺引用', term_inconsistent: '术语不统一',
  structure_incomplete: '结构不完整',
  address_drift: '称呼待确认', setting_conflict: '设定待确认',
  absolute_claim: '绝对化表述', number_unsourced: '数字无出处',
}
const hardIssues = computed(() => store.issues.filter((i) => i.severity !== 'info'))
const softIssues = computed(() => store.issues.filter((i) => i.severity === 'info'))

function ruleLabel(rule) {
  return RULE_LABEL[rule] || rule
}

async function doExtract({ all = false, force = false } = {}) {
  await store.extractGraph({
    nodeId: all ? '' : store.activeNodeId,
    all,
    force,
    limit: all ? 5 : 1,
  })
}

async function submitCard() {
  if (!cardForm.value.name.trim()) return
  const data = cardForm.value.status ? { status: cardForm.value.status } : {}
  const ok = await store.addCard({
    kind: cardForm.value.kind,
    name: cardForm.value.name.trim(),
    summary: cardForm.value.summary.trim(),
    dataJson: JSON.stringify(data),
  })
  if (ok) cardForm.value = { kind: 'character', name: '', summary: '', status: '' }
}

async function doCheck(whole) {
  await store.runCheck({ nodeId: whole ? '' : store.activeNodeId })
  await refreshContext()
}

const tabs = computed(() => {
  const all = [
    { id: 'sources', label: '素材', count: store.sources.length },
    { id: 'facts', label: '事实', count: store.facts.length },
    { id: 'terms', label: '术语', count: store.terms.length },
    { id: 'graph', label: '图谱', count: graphEntities.value.length, novelOnly: true },
    { id: 'context', label: '上下文', count: 0 },
    { id: 'issues', label: '告警', count: store.issues.length },
  ]
  // 单篇文档不建图，隐藏而不是置灰：多一个永远没数据的面板只会让人困惑
  return all.filter((t) => !t.novelOnly || store.isNovel)
})

// 切作品时把三件套与记忆状态拉齐，否则 tab 上的计数会显示成 0，看着像没数据
async function loadAll() {
  await Promise.all([
    store.loadSources(),
    store.loadFacts(),
    store.loadTerms(),
    store.loadIndexStatus(),
    store.loadGraph(),
    store.loadIssueSummary(),
  ])
}
onMounted(loadAll)
watch(() => store.activeWorkId, loadAll)

async function doIndex(force) {
  await store.reindex({ force })
  await refreshContext()
}

async function doSummarize() {
  await store.runSummarize({ scope: 'all' })
  await refreshContext()
}

function barWidth(b) {
  return Math.min(100, Math.round((b.chars / Math.max(b.budget, 1)) * 100)) + '%'
}

async function submitSource() {
  busy.value = true
  try {
    const ok = await store.addSource({ ...src.value })
    if (ok) src.value = { title: '', url: '', published_at: '', content: '' }
  } finally {
    busy.value = false
  }
}

async function submitFact() {
  busy.value = true
  try {
    const ok = await store.addFact({ ...fact.value })
    if (ok) fact.value = { statement: '', valid_until: '' }
  } finally {
    busy.value = false
  }
}

async function submitTerm() {
  busy.value = true
  try {
    const forbidden = term.value.forbiddenText.split(/[,，]/).map((s) => s.trim()).filter(Boolean)
    const ok = await store.addTerm({
      term: term.value.term,
      preferred: term.value.preferred,
      forbidden,
    })
    if (ok) term.value = { term: '', preferred: '', forbiddenText: '' }
  } finally {
    busy.value = false
  }
}

async function refreshContext() {
  loadingCtx.value = true
  try {
    ctx.value = await store.previewContext(props.task)
  } finally {
    loadingCtx.value = false
  }
}

defineExpose({ refreshContext })
</script>

<style scoped>
.panel {
  width: 312px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border-left: 1px solid var(--line-soft);
  min-height: 0;
}
.tabs {
  display: flex;
  gap: 2px;
  padding: 8px 6px 0;
  border-bottom: 1px solid var(--line-soft);
  overflow-x: auto;
}
.tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 9px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-faint);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.tab:hover {
  color: var(--text-muted);
}
.tab.is-active {
  color: var(--run);
  border-bottom-color: var(--run);
}
.tab-count {
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  opacity: 0.7;
}
.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}
.section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hint {
  margin: 0 0 2px;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-faint);
}
.hint.inline {
  margin: 0;
}
.empty {
  margin: 6px 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-faint);
}
.item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 8px 9px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-control);
  background: var(--panel-2);
}
.item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.item-title {
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--text);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.item-meta {
  font-size: 10.5px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.mini-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex-shrink: 0;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.mini-btn:hover {
  background: rgba(255, 255, 255, 0.07);
  color: var(--danger);
}
.add-box {
  margin-top: 4px;
  padding: 8px 10px 10px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-control);
}
.add-box summary {
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
  user-select: none;
}
.add-box .field {
  margin-top: 7px;
}
.add-box .btn {
  margin-top: 8px;
  width: 100%;
}
.ctx-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.ctx-head .btn {
  margin-left: auto;
}

/* 记忆块：索引 / 摘要 */
.mem {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 9px 10px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-control);
  background: var(--panel-2);
}
.mem-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.mem-title {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-muted);
}
.mem-stat {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  color: var(--text-faint);
}
.mem-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.mem-actions .btn {
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
}

/* 检索证据 */
.ev {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 7px 8px;
  border: 1px solid var(--line-soft);
  border-left: 2px solid rgba(217, 164, 91, 0.45);
  border-radius: 6px;
  background: rgba(217, 164, 91, 0.035);
}
.ev-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.ev-title {
  font-size: 11.5px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ev-meta {
  margin-left: auto;
  flex-shrink: 0;
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  color: var(--text-faint);
}
.ev-text {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.bucket {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bucket-top {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  color: var(--text-muted);
}
.bucket-name {
  font-family: theme('fontFamily.mono');
}
.bucket-num {
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.bar {
  height: 4px;
  border-radius: 2px;
  background: var(--ink-700, #1b1f26);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--run);
  opacity: 0.75;
  transition: width 0.3s var(--ease);
}
.bar-fill.over {
  background: var(--warn);
  opacity: 1;
}
.ctx-total {
  margin: 4px 0 0;
  font-size: 11.5px;
  color: var(--text-muted);
  font-family: theme('fontFamily.mono');
}
.warn-line {
  margin: 2px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--warn);
}
.raw {
  margin-top: 6px;
  border-top: 1px solid var(--line-soft);
  padding-top: 6px;
}
.raw summary {
  font-size: 11.5px;
  color: var(--text-faint);
  cursor: pointer;
}
.raw pre {
  margin: 8px 0 0;
  max-height: 260px;
  overflow: auto;
  padding: 9px;
  border-radius: var(--radius-control);
  background: var(--panel-2);
  border: 1px solid var(--line-soft);
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}

/* 图谱：实体按类型分组，状态用淡色标签，不抢主文的注意力 */
.g-kind {
  margin: 10px 0 2px;
  font-size: 10.5px;
  letter-spacing: 0.08em;
  color: var(--text-faint);
}
.g-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 2px;
  font-size: 12.5px;
  color: var(--text);
}
.g-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.g-badge {
  flex-shrink: 0;
  padding: 0 5px;
  border-radius: 3px;
  border: 1px solid rgba(214, 108, 96, 0.35);
  font-size: 10px;
  line-height: 15px;
  color: #d9705f;
}
.g-card-tag {
  margin-left: auto;
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid rgba(217, 164, 91, 0.45);
  color: var(--run);
  font-size: 9px;
  line-height: 12px;
  text-align: center;
}
.g-rel {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 2px;
  font-size: 12px;
  color: var(--text-muted);
}
.g-rt {
  padding: 0 4px;
  border-radius: 3px;
  background: rgba(217, 164, 91, 0.1);
  color: var(--run);
  font-size: 10px;
}
.g-fs {
  display: flex;
  gap: 6px;
  padding: 3px 2px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-muted);
}
.g-fs-ord {
  flex-shrink: 0;
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  color: var(--text-faint);
}

/* 告警：证据与动作与标题同一块，不另开卡片 */
.issue {
  flex-direction: column;
  gap: 5px;
}
.issue-ev {
  margin: 0;
  padding-left: 7px;
  border-left: 2px solid rgba(214, 108, 96, 0.4);
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-faint);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.issue-act {
  display: flex;
  gap: 6px;
}
.issue-act .btn {
  height: 22px;
  padding: 0 8px;
  font-size: 11px;
}
/* 待确认的软规则：左边用中性色，不与硬告警抢注意力 */
.issue.soft {
  border-left: 2px solid rgba(126, 156, 247, 0.35);
}
.issue.soft .item-title {
  color: var(--text-muted);
}
</style>
