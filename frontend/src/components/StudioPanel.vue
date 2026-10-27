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
        <div class="src-top">
          <p class="hint inline">生成时按本节标题与提示词检索素材，命中不了才退回最近导入的；素材桶总预算约 4500 字。</p>
          <button class="btn btn-sm" @click="srcAdding = !srcAdding">
            {{ srcAdding ? '收起' : '+ 添加' }}
          </button>
        </div>

        <!-- 添加：按钮就在眼前，拖文件或粘贴都行；导入后立即建索引 -->
        <div v-if="srcAdding" class="add-box">
          <label class="drop" @dragover.prevent @drop.prevent="onDropFiles">
            <input type="file" multiple accept=".txt,.md,.markdown,.text" hidden @change="onPickFiles" />
            <span>{{ srcBusy ? '导入中…' : '拖入 .txt / .md（可多选），或点这里选文件' }}</span>
          </label>
          <input v-model="src.title" class="field" placeholder="标题（留空取文件名或正文首行）" />
          <input v-model="src.url" class="field" placeholder="来源链接（可选）" />
          <input v-model="src.published_at" class="field" placeholder="资料日期 YYYY-MM（可选）" />
          <textarea v-model="src.content" class="field" rows="6" placeholder="粘贴正文…" />
          <p class="src-count" :class="{ bad: srcChars > 20000 }">
            {{ srcChars }} 字<template v-if="srcChars > 20000"> · 超过 2 万字也没关系：检索只摘命中段落</template>
          </p>
          <button class="btn btn-primary btn-sm" :disabled="!src.content.trim() || srcBusy" @click="submitSource">
            {{ srcBusy ? '导入中…' : '导入' }}
          </button>
          <p v-if="srcError" class="warn-line">{{ srcError }}</p>
        </div>

        <input
          v-model="srcQuery"
          class="field"
          placeholder="在本作品素材里搜一句…（回车）"
          @keyup.enter="searchSources"
        />
        <div v-if="srcHits.length" class="src-hits">
          <p class="hint inline">命中 {{ srcHits.length }} 段，点一条看原文</p>
          <button v-for="h in srcHits" :key="h.chunk_id" class="src-hit" @click="openSource(h.source_id)">
            <span class="src-hit-title">{{ h.title }}</span>
            <span class="src-hit-text">{{ h.text }}</span>
          </button>
          <button class="btn btn-sm" @click="srcHits = []">清除检索</button>
        </div>

        <div v-for="s in store.sources" :key="s.id" class="item src-item" @click="openSource(s.id)">
          <div class="item-main">
            <span class="item-title">{{ s.title || '未命名素材' }}</span>
            <span class="item-meta">
              {{ s.published_at || '无日期' }} · {{ s.chars }} 字
              <template v-if="s.used"> · 已用 {{ s.used }} 次</template>
            </span>
          </div>
          <button class="mini-btn" title="删除" @click.stop="removeSource(s)">
            <AppIcon name="trash" :size="13" />
          </button>
        </div>
        <p v-if="!store.sources.length" class="empty">
          还没有素材。时政/历史类必须导入资料，否则模型会编造事实。
        </p>

        <!-- 单条素材：全文 + 抽事实卡。列表不带正文，一条可能几万字 -->
        <div v-if="srcDetail" class="src-detail">
          <header class="src-detail-head">
            <span class="item-title">{{ srcDetail.title || '未命名素材' }}</span>
            <button class="mini-btn" title="关闭" @click="srcDetail = null">×</button>
          </header>
          <p class="item-meta">
            {{ srcDetail.published_at || '无日期' }} · {{ (srcDetail.content || '').length }} 字
            <template v-if="srcDetail.used"> · 已用 {{ srcDetail.used }} 次</template>
          </p>
          <a v-if="srcDetail.url" class="src-link" :href="srcDetail.url" target="_blank" rel="noreferrer">
            {{ srcDetail.url }}
          </a>
          <pre class="src-content">{{ srcDetail.content }}</pre>
          <div class="src-detail-act">
            <button class="btn btn-sm" :disabled="factsBusy" @click="extractFacts(srcDetail)">
              {{ factsBusy ? '抽取中…' : '抽事实卡' }}
            </button>
            <button class="btn btn-sm" @click="removeSource(srcDetail)">删除</button>
          </div>
          <p v-if="factsNote" class="hint">{{ factsNote }}</p>
        </div>
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
          <!-- 事实挂在素材上，citation_missing / fact_stale 才追得到出处 -->
          <select v-model="fact.source_id" class="field">
            <option value="">不挂素材</option>
            <option v-for="s in store.sources" :key="s.id" :value="s.id">
              {{ s.title || '未命名素材' }}
            </option>
          </select>
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
          <!-- 素材桶现在按本节检索，得让人看得到「这一节到底会引用哪几条」 -->
          <p v-if="ctx.used_sources && ctx.used_sources.length" class="ctx-total">
            引用素材：{{ ctx.used_sources.map((s) => `${s.title}（${s.chars} 字）`).join('、') }}
          </p>
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

      <!-- 历史：每次「采用」与生成落库都存一版，比得了也回得去 -->
      <section v-else-if="tab === 'versions'" class="section">
        <div class="ctx-head">
          <span class="hint inline">
            {{ store.node ? (store.node.title || '当前节') : '未选中节点' }} · {{ store.versions.length }} 版
          </span>
          <button
            class="btn btn-sm"
            :disabled="!store.activeNodeId || store.versionsBusy"
            @click="store.loadVersions()"
          >
            {{ store.versionsBusy ? '读取中…' : '刷新' }}
          </button>
        </div>
        <p class="hint">
          自动保存不留版（那会每几秒塞一条一模一样的）；「采用」、改状态、AI 草稿与润色落库时各存一版。
        </p>
        <div v-for="v in store.versions" :key="v.id" class="item">
          <div class="item-main">
            <span class="item-title">
              版本 #{{ v.version_no }}
              <span v-if="v.version_no === newestNo" class="tag">最新</span>
            </span>
            <span class="item-meta">{{ sourceLabel(v) }} · {{ v.chars }} 字 · {{ fmtTime(v.created_at) }}</span>
          </div>
          <button class="btn btn-sm" :disabled="!store.node" @click="openVersion(v)">对比</button>
        </div>
        <p v-if="!store.activeNodeId" class="empty">先在左边选中一节。</p>
        <p v-else-if="!store.versions.length" class="empty">
          这一节还没有版本。写几段后点「采用」，或把状态改成待审/定稿，就会存下第一版。
        </p>
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

    <VersionDiff
      v-model:open="diffOpen"
      :version="diffVersion"
      :current="store.node?.content || ''"
      @restore="(id) => emit('restore', id)"
    />
  </aside>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import GraphMap from './GraphMap.vue'
import VersionDiff from './VersionDiff.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  task: { type: Object, default: () => ({ task_type: 'article_draft', instruction: '' }) },
})
const emit = defineEmits(['locate', 'restore'])

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
const fact = ref({ statement: '', valid_until: '', source_id: '' })
const term = ref({ term: '', preferred: '', forbiddenText: '' })

// ---- 素材：添加区 / 检索 / 详情 ----
const srcAdding = ref(false)
const srcBusy = ref(false)
const srcError = ref('')
const srcQuery = ref('')
const srcHits = ref([])
const srcDetail = ref(null)
const factsBusy = ref(false)
const factsNote = ref('')
const srcChars = computed(() => (src.value.content || '').length)

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

// ---- 历史（版本） ----
// 来源标签必须与库里的 source 枚举对齐：都是 ai_draft，作者得分清哪条是模型抽风
const SOURCE_LABEL = { ai_draft: 'AI 草稿', ai_polish: 'AI 润色', human: '手改' }
const diffOpen = ref(false)
const diffVersion = ref(null)
const newestNo = computed(() => store.versions[0]?.version_no ?? -1)

function sourceLabel(v) {
  const base = SOURCE_LABEL[v.source] || v.source || '未知'
  return v.run_id ? `${base}·可追生成` : base
}

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

/** 列表不带正文（一节可能几万字），点开对比时再拉那一条的全文 */
async function openVersion(v) {
  const full = await store.loadVersionContent(v.id)
  if (!full) return
  diffVersion.value = full
  diffOpen.value = true
}

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
    { id: 'versions', label: '历史', count: store.versions.length },
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
  srcBusy.value = true
  srcError.value = ''
  try {
    const payload = { ...src.value }
    // 标题留空别让 prompt 里出现《未命名素材》：先取正文首行
    if (!payload.title.trim()) payload.title = titleFromContent(payload.content)
    if (await store.addSource(payload)) {
      src.value = { title: '', url: '', published_at: '', content: '' }
      srcAdding.value = false
    } else {
      srcError.value = store.error || '导入失败'
    }
  } finally {
    srcBusy.value = false
  }
}

function titleFromContent(text) {
  const first = (text || '').split('\n').map((s) => s.trim()).find(Boolean) || ''
  return first.slice(0, 40) || `素材 ${new Date().toLocaleDateString('zh-CN')}`
}

async function onPickFiles(e) {
  await importFiles(Array.from(e.target.files || []))
  e.target.value = ''
}

async function onDropFiles(e) {
  await importFiles(Array.from(e.dataTransfer?.files || []))
}

/** 多选文件逐条导入：.txt/.md 是纯文本，读进来走同一套接口（后端会顺手建索引） */
async function importFiles(files) {
  const list = files.filter((f) => /\.(txt|md|markdown|text)$/i.test(f.name))
  if (!list.length) {
    srcError.value = '只支持 .txt / .md 纯文本；PDF 与 Word 请先另存为文本'
    return
  }
  srcBusy.value = true
  srcError.value = ''
  let ok = 0
  try {
    for (const f of list) {
      const text = await f.text()
      const title = f.name.replace(/\.[^.]+$/, '').slice(0, 80) || titleFromContent(text)
      if (await store.addSource({ title, content: text, url: '', published_at: '' })) ok += 1
    }
  } finally {
    srcBusy.value = false
  }
  if (ok === list.length) {
    srcAdding.value = false
  } else {
    // 添加区要留着，否则这条错误提示跟着一起被藏起来
    srcError.value = `导入 ${ok}/${list.length} 条成功，其余失败：${store.error || '未知原因'}`
  }
}

async function openSource(id) {
  if (!id) return
  factsNote.value = ''
  srcDetail.value = await store.loadSource(id)
  if (!srcDetail.value) srcError.value = store.error || '打不开这条素材'
}

async function removeSource(s) {
  if (!s?.id) return
  const name = s.title || '未命名素材'
  if (!window.confirm(`删除素材「${name}」？\n\n正文与它的索引会一起删掉；已抽的事实卡保留，但会失去出处。`)) return
  await store.deleteSource(s.id)
  if (srcDetail.value?.id === s.id) srcDetail.value = null
}

async function searchSources() {
  const q = srcQuery.value.trim()
  srcHits.value = q ? await store.searchSources(q) : []
}

async function extractFacts(item) {
  if (!item?.id) return
  factsBusy.value = true
  factsNote.value = ''
  try {
    const r = await store.extractFacts(item.id, 5)
    factsNote.value = r
      ? `已抽出 ${r.added} 条事实卡（可在「事实」里改或删）`
      : (store.error || '抽取失败')
  } finally {
    factsBusy.value = false
  }
}

async function submitFact() {
  busy.value = true
  try {
    const ok = await store.addFact({
      ...fact.value,
      source_id: fact.value.source_id || null,
    })
    if (ok) fact.value = { statement: '', valid_until: '', source_id: '' }
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

/* 素材：入口常驻、拖放区、命中片段、详情 */
.src-top {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.src-top .btn {
  flex-shrink: 0;
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
}
.drop {
  display: block;
  padding: 14px 10px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-control);
  text-align: center;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-faint);
  cursor: pointer;
  transition: border-color 0.18s var(--ease), color 0.18s var(--ease);
}
.drop:hover {
  border-color: var(--run);
  color: var(--text-muted);
}
.src-count {
  margin: 6px 0 0;
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.src-count.bad {
  color: var(--warn);
}
.src-hits {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 9px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-control);
  background: var(--panel-2);
}
.src-hit {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 5px 6px;
  border: none;
  border-radius: 4px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.src-hit:hover {
  background: rgba(255, 255, 255, 0.06);
}
.src-hit-title {
  font-size: 11.5px;
  color: var(--run);
}
.src-hit-text {
  font-size: 11px;
  line-height: 1.55;
  color: var(--text-faint);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.src-item {
  cursor: pointer;
  transition: border-color 0.18s var(--ease);
}
.src-item:hover {
  border-color: var(--line);
}
.src-detail {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--panel-2);
}
.src-detail-head {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
.src-detail-head .item-title {
  flex: 1;
  -webkit-line-clamp: 3;
}
.src-link {
  font-size: 10.5px;
  color: var(--run);
  word-break: break-all;
  text-decoration: none;
}
.src-content {
  margin: 0;
  max-height: 260px;
  overflow: auto;
  padding: 8px;
  border-radius: 5px;
  background: var(--ink-700, #1b1f26);
  font-size: 11px;
  line-height: 1.65;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}
.src-detail-act {
  display: flex;
  gap: 6px;
}
.src-detail-act .btn {
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
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
