<template>
  <div class="app-shell">
    <AppNav />

    <div class="studio">
      <!-- 未选作品：书架 -->
      <main v-if="!store.work" class="shelf">
        <header class="shelf-head">
          <div>
            <h1>写作台</h1>
            <p class="shelf-sub">长篇连载与单篇文档共用一套工作台，结构由作品类型决定。</p>
          </div>
          <button class="btn btn-primary" @click="showCreate = !showCreate">
            <AppIcon name="plus" :size="15" /> 新建作品
          </button>
        </header>

        <form v-if="showCreate" class="create card" @submit.prevent="create">
          <div class="create-row">
            <label class="fl">
              <span>类型</span>
              <select v-model="form.kind" class="field" @change="onKindChange">
                <option v-for="k in kinds" :key="k.id" :value="k.id">{{ k.label }}</option>
              </select>
            </label>
            <label v-if="scales.length > 1" class="fl">
              <span>规模</span>
              <select v-model="form.scale" class="field" @change="applyScaleDefaults">
                <option v-for="s in scales" :key="s.id" :value="s.id">{{ s.label }}</option>
              </select>
            </label>
            <label v-if="form.kind === 'novel'" class="fl">
              <span>题材</span>
              <input v-model="form.subtype" class="field" placeholder="玄幻 / 都市 / 科幻…" />
            </label>
            <label v-else-if="subtypes.length" class="fl">
              <span>文体</span>
              <select v-model="form.subtype" class="field">
                <option v-for="s in subtypes" :key="s.id" :value="s.id">{{ s.label }}</option>
              </select>
            </label>
            <label class="fl grow">
              <span>标题</span>
              <input v-model="form.title" class="field" required placeholder="作品标题" />
            </label>
          </div>
          <label class="fl grow">
            <span>一句话主旨</span>
            <textarea v-model="form.premise" class="field" rows="2"
                      placeholder="这篇/这本书要讲什么，给谁看，最重要的一句话是什么" />
          </label>
          <div class="create-foot">
            <label class="fl small">
              <span>目标字数</span>
              <input v-model.number="form.target_words" class="field" type="number" min="0" step="1000" />
            </label>
            <p v-if="store.error" class="err">{{ store.error }}</p>
            <button class="btn btn-primary" type="submit" :disabled="creating">
              {{ creating ? '创建中…' : '创建并进入' }}
            </button>
          </div>
        </form>

        <div v-if="store.works.length" class="grid">
          <div
            v-for="w in store.works"
            :key="w.id"
            class="work-card"
            role="button"
            tabindex="0"
            @click="openWork(w.id)"
            @keydown.enter="openWork(w.id)"
          >
            <button class="work-del" title="删除作品（连同节点树）" @click.stop="removeWork(w)">
              <AppIcon name="trash" :size="12" />
            </button>
            <span class="work-kind">{{ kindLabel(w) }}</span>
            <span class="work-title">{{ w.title }}</span>
            <span class="work-meta">{{ w.node_count }} 节 · {{ fmt(w.words) }} 字</span>
            <span class="work-premise">{{ w.premise || '还没有主旨' }}</span>
          </div>
        </div>
        <p v-else-if="!showCreate" class="shelf-empty">
          还没有作品。点右上角「新建作品」，或先把已有稿件粘进来改写。
        </p>
      </main>

      <!-- 已选作品：三栏工作台 -->
      <template v-else>
        <StudioTree
          :tree="store.tree"
          :active-id="store.activeNodeId"
          :title="store.work.title"
          :stats="store.stats"
          @select="store.selectNode"
          @create="quickCreate"
          @remove="removeNode"
          @move="moveNode"
        />

        <!-- 上下移的结果：到边了不是报错，给一条会自己消掉的提示 -->
        <div v-if="moveNote" class="move-note" role="status">{{ moveNote }}</div>

        <main class="editor">
          <header class="ed-top">
            <button class="icon-btn" title="返回书架" @click="backToShelf">
              <AppIcon name="library" :size="16" />
            </button>
            <span class="ed-work">{{ store.work.title }}</span>
            <span v-if="store.template" class="pill pill--info">{{ store.template.name }}</span>
            <span class="ed-save">{{ saveLabel }}</span>
            <div class="ed-right">
              <button class="btn btn-sm" :disabled="store.outlineBusy || store.pipeline.running"
                      title="按模板和主旨生成目录，落成节点" @click="doOutline">
                <AppIcon name="layers" :size="13" />
                {{ store.outlineBusy ? '大纲中…' : '大纲' }}
              </button>
              <button class="btn btn-sm" :disabled="store.pipeline.running"
                      title="把还没有正文的节逐节生成" @click="doPipeline">
                <AppIcon name="zap" :size="13" /> 整篇
              </button>
              <button class="btn btn-sm" :disabled="store.queueBusy"
                      title="排进队列，关页面也会继续跑" @click="doEnqueue">
                <AppIcon name="activity" :size="13" /> 排队
              </button>
              <button class="btn btn-sm" title="拼装成 Markdown 并下载" @click="store.downloadExport()">
                <AppIcon name="download" :size="13" /> 导出
              </button>
              <span class="ed-model">deepseek-chat</span>
            </div>
          </header>

          <div v-if="!store.node" class="ed-empty">
            <AppIcon name="pen" :size="20" />
            <h2>选一个节点开始</h2>
            <p>左侧点「+」建卷/章，或在空白作品里直接建第一节。</p>
          </div>

          <template v-else>
            <div class="ed-head">
              <input v-model="titleDraft" class="ed-title" aria-label="节点标题"
                     @change="saveTitle" placeholder="未命名" />
              <select class="field status-select" :value="store.node.status" @change="changeStatus">
                <option value="draft">草稿</option>
                <option value="review">待审</option>
                <option value="final">定稿</option>
              </select>
              <span class="ed-count">{{ wordCount }} 字</span>
            </div>

            <div class="ed-body">
              <textarea
                ref="editorEl"
                v-model="contentDraft"
                class="ed-textarea"
                spellcheck="false"
                placeholder="在这里写，或点下方「生成」让 AI 出草稿…"
                @input="onInput"
              />
            </div>

            <!-- 生成预览：先看，点采用才写回 -->
            <section v-if="store.generating || store.genText || store.genError" class="gen">
              <header class="gen-head">
                <span class="gen-title">
                  <span v-if="store.generating" class="spinner" aria-hidden="true"></span>
                  {{ genStatus }}
                </span>
                <span class="gen-meta">{{ genMetaText }}</span>
              </header>
              <pre v-if="store.genText" class="gen-text">{{ store.genText }}</pre>
              <p v-if="store.genError" class="gen-err">{{ store.genError }}</p>
              <footer v-if="!store.generating && store.genText" class="gen-actions">
                <button class="btn btn-primary btn-sm" @click="adopt">
                  <AppIcon name="check" :size="13" /> 采用
                </button>
                <button class="btn btn-sm" @click="store.discardGenerated()">放弃</button>
                <span class="gen-tip">
                  采用后会存为一个版本，可在右栏「历史」里对比并回滚
                </span>
              </footer>
            </section>

            <!-- 整篇流水线进度：逐节跑，不阻塞看进度 -->
            <section v-if="showPipeline" class="pipe">
              <header class="pipe-head">
                <span class="pipe-title">
                  <span v-if="store.pipeline.running" class="spinner" aria-hidden="true"></span>
                  {{ pipeTitle }}
                </span>
                <span class="pipe-meta">
                  已完成 {{ store.pipeline.done }}/{{ store.pipeline.total }} 节 · {{ store.pipeline.chars }} 字
                  <template v-if="store.pipeline.indexed"> · 索引 {{ store.pipeline.indexed }}</template>
                  <template v-if="store.pipeline.summarized"> · 摘要 {{ store.pipeline.summarized }}</template>
                </span>
                <button v-if="!store.pipeline.running" class="mini" @click="resetPipeline">收起</button>
              </header>
              <div class="bar"><div class="bar-fill" :style="{ width: pipePercent }"></div></div>
              <pre v-if="store.pipeline.preview" class="pipe-preview">{{ store.pipeline.preview }}</pre>
              <p v-if="store.pipeline.error" class="pipe-err">{{ store.pipeline.error }}</p>
            </section>

            <!-- 队列：落库的任务，断网与重启都不影响 -->
            <section v-if="showQueue" class="pipe queue">
              <header class="pipe-head">
                <span class="pipe-title">
                  <span v-if="store.queue?.current" class="spinner" aria-hidden="true"></span>
                  {{ queueTitle }}
                </span>
                <span class="pipe-meta">
                  排队 {{ qc.queued || 0 }} · 完成 {{ qc.done || 0 }}
                  <template v-if="qc.failed"> · 失败 {{ qc.failed }}</template>
                  <template v-if="store.queue?.eta_sec">
                    · 约 {{ Math.ceil(store.queue.eta_sec / 60) }} 分钟
                  </template>
                </span>
                <button v-if="store.queue?.paused" class="mini" @click="resumeQ">继续</button>
                <button v-else class="mini" @click="pauseQ">暂停</button>
                <button class="mini" @click="queueHidden = true">收起</button>
              </header>
              <p v-if="store.queue?.paused" class="pipe-err">已暂停：{{ store.queue.reason }}</p>
              <div v-if="store.queue?.current" class="bar">
                <div class="bar-fill indeterminate"></div>
              </div>

              <!-- 抽检卡点：连续两节判「重写」就整队停下 -->
              <div v-if="store.queue?.triage?.due" class="triage">
                <p class="triage-title">抽检：这两节能用吗？连续两次「重写」会暂停整条队列</p>
                <div class="triage-list">
                  <div v-for="s in store.queue.triage.samples.slice(0, 2)" :key="s.node_id" class="triage-item">
                    <span class="triage-name">{{ s.title || s.node_id }}</span>
                    <span class="triage-score">{{ s.score ?? '—' }}</span>
                  </div>
                </div>
                <div class="triage-act">
                  <button class="btn btn-sm" @click="doTriage('ok')">可用，继续</button>
                  <button class="btn btn-sm" @click="doTriage('rewrite')">要重写</button>
                </div>
              </div>
            </section>

            <footer class="gen-bar">
              <select v-model="taskType" class="field task-select">
                <option value="article_draft">写本节</option>
                <option value="continue">续写</option>
                <option value="polish">润色</option>
              </select>
              <input
                v-model="instruction"
                class="field gen-input"
                placeholder="可选：这一节要写什么（大纲/整篇也用这句）"
                @keydown.enter.prevent="runGenerate"
              />
              <button class="btn btn-sm" title="对选中文字改写/压缩/扩写，多候选对比" @click="openTransform">
                <AppIcon name="pen" :size="13" /> 加工
              </button>
              <button class="btn btn-primary" :disabled="store.generating" @click="runGenerate">
                <AppIcon name="spark" :size="14" />
                {{ store.generating ? '生成中' : '生成' }}
              </button>
            </footer>
          </template>
        </main>

        <StudioPanel
          ref="panelRef"
          :task="{ task_type: taskType, instruction }"
          @locate="locateEvidence"
          @restore="onRestoreVersion"
        />
      </template>
    </div>

    <TransformDialog
      v-model:open="transformOpen"
      :text="contentDraft"
      :selection="selectedText"
      @adopt="onTransformAdopt"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppNav from '../components/AppNav.vue'
import AppIcon from '../components/AppIcon.vue'
import StudioTree from '../components/StudioTree.vue'
import StudioPanel from '../components/StudioPanel.vue'
import TransformDialog from '../components/TransformDialog.vue'
import { useStudioStore } from '../stores/studio'
import { apiFetch } from '../utils/api'

const store = useStudioStore()
const route = useRoute()
const router = useRouter()

const showCreate = ref(false)
const creating = ref(false)
const form = ref({ kind: 'article', scale: 'single', subtype: 'tech', title: '', premise: '', target_words: 5000 })

const titleDraft = ref('')
const contentDraft = ref('')
const saveState = ref('saved') // saved | saving | dirty
const taskType = ref('article_draft')
const instruction = ref('')
const editorEl = ref(null)
const panelRef = ref(null)

// 类型下拉来自 /studio/content-types（content-types.yaml），新增文种只改 YAML
const kinds = computed(() =>
  store.contentTypes.length
    ? store.contentTypes
    : [{ id: 'article', label: '文档', scales: [{ id: 'single', label: '单篇' }], subtypes: [] }],
)
const currentKind = computed(() => kinds.value.find((k) => k.id === form.value.kind) || null)
const scales = computed(() => currentKind.value?.scales || [])
const subtypes = computed(() => currentKind.value?.subtypes || [])

const transformOpen = ref(false)
const selectedText = ref('')

// ---- 队列（M4） ----
const queueHidden = ref(false)
const qc = computed(() => store.queue?.counts || {})
const showQueue = computed(() => {
  if (queueHidden.value) return false
  const q = store.queue
  if (!q) return false
  return store.queueActive || (q.counts?.done || 0) + (q.counts?.failed || 0) > 0
})
const queueTitle = computed(() => {
  const q = store.queue
  if (!q) return '队列'
  if (q.triage?.due) return '等待抽检'
  if (q.paused) return '队列已暂停'
  if (q.current) return `正在写：${q.current.title || q.current.task_type}`
  return `队列已排空 · 完成 ${q.counts?.done || 0} 节`
})

const showPipeline = computed(
  () => store.pipeline.running || store.pipeline.total > 0 || Boolean(store.pipeline.error),
)
const pipeTitle = computed(() => {
  const p = store.pipeline
  if (p.running) return p.title ? `正在生成：${p.title}` : '准备中…'
  return `整篇已生成 ${p.done}/${p.total} 节`
})
const pipePercent = computed(() => {
  const p = store.pipeline
  if (!p.total) return '0%'
  return Math.min(100, Math.round((p.done / p.total) * 100)) + '%'
})

function onKindChange() {
  applyScaleDefaults()
  form.value.subtype = subtypes.value[0]?.id || ''
}

function applyScaleDefaults() {
  form.value.scale = scales.value[0]?.id || 'single'
  const s = scales.value.find((x) => x.id === form.value.scale)
  if (s?.target_words) form.value.target_words = s.target_words
}

let saveTimer = 0

const saveLabel = computed(() => {
  if (!store.node) return ''
  if (store.saving || saveState.value === 'saving') return '保存中…'
  if (saveState.value === 'dirty') return '未保存'
  return '已保存'
})

const wordCount = computed(() => {
  const text = contentDraft.value || ''
  const cjk = (text.match(/[\u4e00-\u9fff]/g) || []).length
  const words = text.replace(/[\u4e00-\u9fff]/g, ' ').split(/\s+/).filter(Boolean).length
  return cjk + words
})

const genStatus = computed(() => {
  if (store.generating) return store.genText ? '生成中' : '装配上下文…'
  return '生成完成'
})

const genMetaText = computed(() => {
  const n = store.genText.length
  if (!store.generating) return `${n} 字`
  // 实测约 140 字/秒，给用户一个可预期的剩余时间
  const remain = Math.max(1, Math.round((2800 - n) / 140))
  return `${n} 字 · 约 ${remain}s`
})

function fmt(n) {
  if (!n) return '0'
  return n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n)
}

/** 把告警定位到正文：按证据原文找，找不到就退到前 24 / 前 12 字。 */
function locateEvidence(text) {
  const el = editorEl.value
  const full = contentDraft.value || ''
  if (!el || !text || !full) return false
  const needles = [text, text.slice(0, 24), text.slice(0, 12)].filter(Boolean)
  for (const needle of needles) {
    const at = full.indexOf(needle)
    if (at < 0) continue
    el.focus()
    el.setSelectionRange(at, at + needle.length)
    // 浏览器会把光标滚到可视区，但常常只差一点点，手工再校准一次
    const ratio = at / Math.max(full.length, 1)
    el.scrollTop = Math.max(0, ratio * el.scrollHeight - el.clientHeight / 3)
    return true
  }
  return false
}

function kindLabel(w) {
  if (w.kind === 'novel') return '长篇'
  return w.scale === 'series' ? '系列' : (w.subtype || '单篇')
}

onMounted(async () => {
  await Promise.all([store.fetchWorks(), store.loadTemplates('article'), store.loadContentTypes()])
  if (!kinds.value.some((k) => k.id === form.value.kind)) form.value.kind = kinds.value[0]?.id || 'article'
  applyScaleDefaults()
  const id = route.params.id
  if (id) await openWork(id)
})

watch(() => route.params.id, (id) => {
  if (id && id !== store.activeWorkId) openWork(id)
})

watch(() => store.node?.id, () => {
  titleDraft.value = store.node?.title || ''
  contentDraft.value = store.node?.content || ''
  saveState.value = 'saved'
})

// 键盘可达：Ctrl+S 立即存，Ctrl+Enter 生成（见 design-system MASTER.md）
function onKeydown(e) {
  if (!(e.ctrlKey || e.metaKey)) return
  if (e.key === 's') {
    e.preventDefault()
    flushSave()
  } else if (e.key === 'Enter') {
    e.preventDefault()
    runGenerate()
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  stopQueuePoll()
  clearTimeout(moveNoteTimer)
})

async function openWork(id) {
  await store.selectWork(id)
  await store.loadTemplates(store.work?.kind || 'article')
  if (route.params.id !== id) router.replace(`/studio/${id}`)
  // 队列可能在关页面期间一直在跑，回来先把状态拉齐
  queueHidden.value = false
  await store.loadQueue()
  if (store.queueActive) startQueuePoll()
  // 默认选中第一个有正文的节点，省一次点击
  const first = store.tree.find((n) => n.ordinal != null)
  if (first) await store.selectNode(first.id)
}

function backToShelf() {
  router.replace('/studio')
  store.work = null
  store.tree = []
  store.node = null
}

async function removeWork(w) {
  if (!window.confirm(`删除《${w.title}》？节点与正文会一并删除，不可撤销。`)) return
  await store.deleteWork(w.id)
}

async function create() {
  creating.value = true
  store.error = ''
  try {
    const w = await store.createWork({ ...form.value })
    showCreate.value = false
    form.value = { kind: 'article', scale: 'single', subtype: 'tech', title: '', premise: '', target_words: 5000 }
    router.replace(`/studio/${w.id}`)
  } catch (e) {
    store.error = String(e?.message || e).slice(0, 200)
  } finally {
    creating.value = false
  }
}

async function quickCreate(parent) {
  const depth = parent ? parent.depth + 1 : 0
  const kind = depth === 0 ? (store.work.kind === 'novel' ? 'volume' : 'section') : 'section'
  const title = kind === 'volume' ? `第 ${store.tree.filter((n) => n.depth === 0).length + 1} 卷` : '新节点'
  const n = await store.createNode({ kind, title, parent_id: parent?.id || null })
  if (n) titleDraft.value = n.title
}

async function removeNode(node) {
  if (!window.confirm(`删除「${node.title}」及其子节点？此操作不可撤销。`)) return
  await store.deleteNode(node.id)
}

// ---- 同层上下移 ----
const moveNote = ref('')
let moveNoteTimer = 0

function showMoveNote(text) {
  moveNote.value = text
  clearTimeout(moveNoteTimer)
  moveNoteTimer = setTimeout(() => (moveNote.value = ''), 2600)
}

/**
 * 上下移一节：与紧邻的前/后兄弟互换位置。
 *
 * 先 flushSave：交换会把两节的章号连同图谱上的章号引用一起对调，编辑器里那份
 * 还没落盘的正文不先存上，刷出来的树会与库里对不上。
 * 移完重跑一次一致性检查——那套规则按章号判定（伏笔超期、死后还出场），
 * 顺序一变结论就可能变，纯本地比对不调模型。告警里的章号文本要等下一次检查才刷新。
 */
async function moveNode(node, direction) {
  await flushSave()
  const r = await store.moveNode(node.id, direction)
  if (!r.moved) {
    showMoveNote(r.reason || '移动失败')
    return
  }
  showMoveNote(`已与「${r.swapped_with_title}」交换位置`)
  await store.runCheck()
  await panelRef.value?.refreshContext()
}

function onInput() {
  saveState.value = 'dirty'
  clearTimeout(saveTimer)
  saveTimer = setTimeout(flushSave, 800)
}

/** 立即落盘；已经是最新状态就不重复请求 */
async function flushSave() {
  if (!store.node) return
  clearTimeout(saveTimer)
  if (saveState.value === 'saved') return
  saveState.value = 'saving'
  await store.saveNode({ content: contentDraft.value })
  saveState.value = 'saved'
}

async function saveTitle() {
  if (!titleDraft.value.trim()) return
  await store.saveNode({ title: titleDraft.value.trim() })
}

async function changeStatus(e) {
  await store.saveNode({ status: e.target.value }, { snapshot: true })
}

async function runGenerate() {
  if (store.generating || !store.node) return
  // 有未保存改动先落盘，否则装配用的是旧正文
  await flushSave()
  await store.generate({ task_type: taskType.value, instruction: instruction.value })
}

async function adopt() {
  const append = taskType.value === 'continue'
  await store.adoptGenerated({ append })
  contentDraft.value = store.node?.content || ''
}

/**
 * 回滚到某个历史版本。先把编辑器里那份 flushSave 掉，再让服务端写回——
 * 顺序反了的话，服务端看到的是上一版自动保存的正文，手里这份改动就直接丢了。
 */
async function onRestoreVersion(versionId) {
  await flushSave()
  const ok = await store.restoreVersion(versionId)
  if (!ok) return
  contentDraft.value = store.node?.content || ''
  saveState.value = 'saved'
  // 正文换了，上下文预览（含摘要过期提示）得跟着重装配一次，纯本地不调模型
  await panelRef.value?.refreshContext()
}

// ==================== 大纲 / 整篇 / 加工 ====================

async function doOutline() {
  if (!store.work) return
  const hasNodes = store.tree.some((n) => n.ordinal != null)
  const append = hasNodes
    ? window.confirm('已有内容节点。确认追加一套新目录吗？（取消则中止）')
    : false
  if (hasNodes && !append) return
  const r = await store.generateOutline({ instruction: instruction.value, append })
  if (r) instruction.value = ''
}

async function doPipeline() {
  if (store.pipeline.running || !store.work) return
  await flushSave()
  resetPipeline()
  await store.runPipeline({ instruction: instruction.value })
}

// ---- 队列（M4） ----
let queueTimer = 0

function startQueuePoll() {
  if (queueTimer) return
  queueTimer = setInterval(async () => {
    await store.loadQueue()
    if (!store.queueActive) stopQueuePoll()
    // 单节写完后正文被换掉了，没选中的情况下也刷新一下当前节点
    if (store.queue?.current == null && store.node) {
      const r = await fetchNodeQuietly(store.node.id)
      if (r && r.content !== contentDraft.value) applyRemoteContent(r)
    }
  }, 3000)
}

function stopQueuePoll() {
  if (queueTimer) clearInterval(queueTimer)
  queueTimer = 0
}

async function fetchNodeQuietly(id) {
  try {
    const r = await apiFetch(`/studio/nodes/${id}`)
    return r.ok ? await r.json() : null
  } catch {
    return null
  }
}

/** 队列写完后把正文换成服务端版本（只在本地没有未保存修改时） */
function applyRemoteContent(node) {
  if (saveState.value === 'dirty' || store.saving) return
  contentDraft.value = node.content || ''
  store.node = node
  saveState.value = 'saved'
}

async function doEnqueue() {
  if (!store.work) return
  await flushSave()
  const j = await store.enqueueBatch({ instruction: instruction.value })
  if (!j) return
  queueHidden.value = false
  startQueuePoll()
  if (store.node) await store.selectNode(store.node.id)
}

async function pauseQ() {
  await store.pauseQueue('手动暂停')
}

async function resumeQ() {
  await store.resumeQueue()
  startQueuePoll()
}

async function doTriage(verdict) {
  await store.triageQueue(verdict)
  if (store.queueActive) startQueuePoll()
}

function resetPipeline() {
  store.pipeline.total = 0
  store.pipeline.done = 0
  store.pipeline.chars = 0
  store.pipeline.preview = ''
  store.pipeline.error = ''
  store.pipeline.title = ''
  store.pipeline.indexed = 0
  store.pipeline.summarized = 0
}

function openTransform() {
  if (!store.node) return
  const el = editorEl.value
  let sel = ''
  if (el && el.selectionStart !== el.selectionEnd) {
    sel = contentDraft.value.slice(el.selectionStart, el.selectionEnd)
  }
  selectedText.value = sel
  transformOpen.value = true
}

/** 采用加工结果：有选区就替换选区，否则整节替换 */
function onTransformAdopt(text) {
  const el = editorEl.value
  if (el && el.selectionStart !== el.selectionEnd) {
    const s = el.selectionStart
    const e = el.selectionEnd
    contentDraft.value = contentDraft.value.slice(0, s) + text + contentDraft.value.slice(e)
  } else {
    contentDraft.value = text
  }
  saveState.value = 'dirty'
  flushSave()
}
</script>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: var(--bg);
}
.studio {
  flex: 1;
  min-width: 0;
  display: flex;
  background: var(--bg);
}

/* ---------- 书架 ---------- */
.shelf {
  flex: 1;
  overflow-y: auto;
  padding: 34px 40px 60px;
  max-width: 1080px;
  margin: 0 auto;
  width: 100%;
}
.shelf-head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 22px;
}
.shelf-head h1 {
  margin: 0 0 5px;
  font-family: theme('fontFamily.serif');
  font-size: 26px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text);
}
.shelf-sub {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-faint);
}
.shelf-head .btn {
  margin-left: auto;
}
.create {
  padding: 16px 18px;
  margin-bottom: 22px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.create-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.fl {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.fl > span {
  font-size: 11px;
  color: var(--text-faint);
}
.fl .field {
  width: 132px;
}
.fl.grow {
  flex: 1;
  min-width: 200px;
}
.fl.grow .field,
.fl > .field.grow {
  width: 100%;
}
.fl.small .field {
  width: 110px;
}
.create-foot {
  display: flex;
  align-items: flex-end;
  gap: 12px;
}
.create-foot .btn {
  margin-left: auto;
}
.err {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
.work-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 15px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.work-del {
  position: absolute;
  top: 8px;
  right: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s var(--ease), color 0.15s var(--ease);
}
.work-card:hover .work-del,
.work-card:focus-within .work-del {
  opacity: 1;
}
.work-del:hover {
  color: var(--danger);
  background: rgba(228, 120, 120, 0.1);
}
.work-card:hover {
  border-color: rgba(217, 164, 91, 0.45);
  background: var(--panel-2);
}
.work-kind {
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--run);
}
.work-title {
  font-family: theme('fontFamily.serif');
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
.work-meta {
  font-size: 11px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.work-premise {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.shelf-empty {
  margin-top: 30px;
  font-size: 13px;
  color: var(--text-faint);
}

/* ---------- 编辑器 ---------- */
.editor {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
.ed-top {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 16px;
  border-bottom: 1px solid var(--line-soft);
  background: rgba(17, 19, 24, 0.7);
}
.ed-work {
  font-size: 12.5px;
  color: var(--text-muted);
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ed-save {
  font-size: 11px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.ed-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ed-right .btn {
  height: 26px;
  padding: 0 9px;
  font-size: 11.5px;
  gap: 4px;
}
.ed-model {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
  padding: 3px 8px;
  border: 1px solid var(--line-soft);
  border-radius: 999px;
}
.ed-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--text-faint);
}
.ed-empty h2 {
  margin: 4px 0 0;
  font-size: 15px;
  font-weight: 500;
  color: var(--text-muted);
}
.ed-empty p {
  margin: 0;
  font-size: 12.5px;
}
.ed-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 0 8px;
  /* 与正文同宽：38em @ 17px */
  width: min(646px, calc(100% - 64px));
  margin: 0 auto;
}
.ed-title {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  font-family: theme('fontFamily.serif');
  font-size: 24px;
  font-weight: 600;
  color: var(--text);
  padding: 2px 0;
}
.ed-title:focus {
  outline: none;
  border-bottom: 1px solid var(--line);
}
.status-select {
  width: 84px;
  height: 28px;
  font-size: 12px;
}
.ed-count {
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
}
.ed-body {
  flex: 1;
  min-height: 0;
  width: 100%;
  display: flex;
  justify-content: center;
  padding: 0 32px;
}
.ed-textarea {
  width: min(646px, 100%);
  height: 100%;
  resize: none;
  border: none;
  background: transparent;
  color: var(--text);
  font-family: theme('fontFamily.serif');
  font-size: 17px;
  line-height: 1.9;
  letter-spacing: 0.01em;
  padding: 8px 0 30px;
  outline: none;
}
.ed-textarea::placeholder {
  color: var(--text-faint);
  font-family: theme('fontFamily.sans');
  font-size: 13.5px;
  line-height: 1.7;
}

/* ---------- 上下移结果提示（浮在树外角，不插进布局，不然行高会跳） ---------- */
.move-note {
  position: fixed;
  left: 16px;
  bottom: 16px;
  z-index: 90;
  max-width: 240px;
  padding: 7px 11px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.6;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.32);
}

/* ---------- 生成预览 ---------- */
.gen {
  margin: 0 32px;
  border: 1px solid rgba(217, 164, 91, 0.28);
  border-radius: var(--radius-panel);
  background: rgba(217, 164, 91, 0.045);
  padding: 10px 14px 12px;
  max-height: 34vh;
  display: flex;
  flex-direction: column;
}
.gen-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.gen-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.gen-meta {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.spinner {
  width: 10px;
  height: 10px;
  border: 1.6px solid rgba(217, 164, 91, 0.3);
  border-top-color: var(--run);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.gen-text {
  margin: 8px 0 0;
  overflow-y: auto;
  font-family: theme('fontFamily.serif');
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.gen-err {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--danger);
}
.gen-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 9px;
}
.gen-tip {
  font-size: 11px;
  color: var(--text-faint);
}

/* ---------- 整篇流水线进度 ---------- */
.pipe {
  margin: 0 32px 8px;
  padding: 9px 12px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.pipe-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.pipe-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
}
.pipe-meta {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.pipe .bar {
  height: 4px;
  border-radius: 2px;
  background: var(--line-soft);
  overflow: hidden;
}
.pipe .bar-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--run);
  opacity: 0.8;
  transition: width 0.3s var(--ease);
}
.pipe-preview {
  margin: 0;
  max-height: 14vh;
  overflow-y: auto;
  font-family: theme('fontFamily.serif');
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}
.pipe-err {
  margin: 0;
  font-size: 11.5px;
  color: var(--danger);
}

/* 队列：不确定进度用一条缓慢来回的光带，比假百分比诚实 */
.queue {
  border-color: rgba(217, 164, 91, 0.28);
}
.pipe .bar-fill.indeterminate {
  width: 38%;
  background: linear-gradient(90deg, transparent, var(--run), transparent);
  animation: queue-sweep 1.6s linear infinite;
}
@keyframes queue-sweep {
  from {
    margin-left: -38%;
  }
  to {
    margin-left: 100%;
  }
}
.triage {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 9px 10px;
  border: 1px solid rgba(217, 164, 91, 0.35);
  border-radius: var(--radius-control);
  background: rgba(217, 164, 91, 0.05);
}
.triage-title {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
.triage-list {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.triage-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  color: var(--text);
}
.triage-score {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--run);
}
.triage-act {
  display: flex;
  gap: 6px;
}
.mini {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  font-size: 11px;
  height: 22px;
  padding: 0 7px;
  cursor: pointer;
}
.mini:hover {
  color: var(--text);
  border-color: var(--line-strong);
}

/* ---------- 生成工具条 ---------- */
.gen-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 32px 14px;
  border-top: 1px solid var(--line-soft);
  background: rgba(17, 19, 24, 0.8);
}
.task-select {
  width: 108px;
  flex-shrink: 0;
}
.gen-input {
  flex: 1;
}
.gen-bar .btn {
  flex-shrink: 0;
}

@media (max-width: 1200px) {
  .shelf {
    padding: 24px 20px 50px;
  }
  .ed-head,
  .ed-textarea {
    width: 100%;
  }
  .gen-bar {
    padding: 10px 16px 14px;
  }
  .gen {
    margin: 0 16px;
  }
  .pipe {
    margin: 0 16px 8px;
  }
}

/* 动效尊重系统偏好 */
@media (prefers-reduced-motion: reduce) {
  .spinner {
    animation-duration: 3s;
  }
  .work-card,
  .ed-textarea {
    transition: none;
  }
}
</style>
