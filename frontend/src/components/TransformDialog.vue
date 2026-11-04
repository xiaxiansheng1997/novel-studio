<template>
  <div v-if="open" class="mask" @click.self="close">
    <section class="dlg" role="dialog" aria-modal="true" aria-label="加工选中文字">
      <header class="dlg-head">
        <div>
          <h2>加工</h2>
          <p class="dlg-sub">
            把一段文字交给模型改写；多个候选横向比，整段挑一个或逐段混合写回。
          </p>
        </div>
        <button class="icon-btn" title="关闭" @click="close">
          <AppIcon name="x" :size="15" />
        </button>
      </header>

      <div class="dlg-body">
        <div class="row">
          <label class="fl">
            <span>动作</span>
            <select v-model="action" class="field">
              <option v-for="a in actions" :key="a.id" :value="a.id">{{ a.label }}</option>
            </select>
          </label>
          <label class="fl">
            <span>候选数</span>
            <select v-model.number="candidates" class="field narrow" :disabled="!!compareModels.length">
              <option :value="1">1 个</option>
              <option :value="2">2 个</option>
              <option :value="3">3 个（慢）</option>
            </select>
          </label>
          <label v-if="action === 'translate'" class="fl">
            <span>目标语言</span>
            <input v-model="lang" class="field narrow" placeholder="英文" />
          </label>
          <label class="fl grow">
            <span>附加要求（可选）</span>
            <input v-model="instruction" class="field" placeholder="例如：更口语一点 / 面向初学者" />
          </label>
        </div>

        <!-- 跨模型对比：同一家抽第二稿的差异，往往不如换一家的手感大 -->
        <div v-if="modelChips.length && !titleMode" class="models">
          <span class="models-label">对比模型
            <em v-if="compareModels.length">已选 {{ compareModels.length }} 家，每家各出 1 条（候选数不生效）</em>
            <em v-else>可不选；选几家就每家出一条横着比</em>
          </span>
          <div class="chips">
            <button
              v-for="m in modelChips"
              :key="m.id"
              type="button"
              class="chip"
              :class="{ on: compareModels.includes(m.id) }"
              @click="toggleCompare(m.id)"
            >{{ m.label }}</button>
          </div>
        </div>

        <label class="fl">
          <span>
            原文 <em class="cnt">{{ srcChars }} 字</em>
            <button v-if="hasSelection && !titleMode" class="link" @click="useSelection">用编辑器里选中的那段</button>
            <button v-else-if="titleMode && text !== fullText" class="link" @click="text = fullText">用整节正文</button>
          </span>
          <textarea v-model="text" class="field src" rows="6" placeholder="要加工的文字" />
        </label>

        <div class="run-row">
          <p v-if="store.error" class="err">{{ store.error }}</p>
          <p v-if="store.transformResult?.errors?.length" class="err">
            {{ store.transformResult.errors.join('；') }}
          </p>
          <button
            class="btn btn-primary"
            :disabled="!text.trim() || store.transforming"
            @click="run"
          >
            <AppIcon name="spark" :size="14" />
            {{ store.transforming ? '加工中…' : '开始' }}
          </button>
        </div>

        <div v-if="store.transforming" class="waiting">
          <span class="spinner" aria-hidden="true"></span>
          正在生成 {{ compareModels.length ? `${compareModels.length} 家模型各 1 个` : `${candidates} 个` }}候选，单个约 5-20 秒
        </div>

        <p v-if="titleMode" class="dlg-tip">点一行就能回填；左边进本节标题，右边进作品标题（导出时那一句 `# 标题`）。</p>

        <!-- 逐段混合：整段采用只挑一个候选，但好的往往每家只占其中几段 -->
        <div v-if="results.length && !titleMode" class="res-bar">
          <button v-if="results.length > 1" class="link" @click="toggleMix">
            {{ mixOn ? '改看完整候选' : '逐段混合' }}
          </button>
          <span v-if="mixOn" class="res-hint">每段从各候选里挑一个；「原文」这段不动，「丢弃」这段不要</span>
        </div>
        <div v-if="mixOn && !titleMode" class="mix">
          <section v-for="(slot, i) in mixSlots" :key="'ms' + i" class="mix-slot">
            <h3 class="mix-no">第 {{ i + 1 }} 段</h3>
            <div class="mix-opts">
              <button
                v-for="opt in slot.opts"
                :key="opt.key"
                type="button"
                class="mix-opt"
                :class="{ on: mixPick[i] === opt.key }"
                @click="pickSlot(i, opt.key)"
              >
                <span class="mix-tag">{{ opt.tag }}</span>
                <span class="mix-text">{{ opt.text || '（这段不要）' }}</span>
              </button>
            </div>
          </section>
          <footer class="mix-foot">
            <button class="btn btn-primary btn-sm" :disabled="!mixComposed" @click="adopt(mixComposed)">
              <AppIcon name="check" :size="13" /> 写回 {{ mixUsed }}/{{ mixTotal }} 段
            </button>
            <button class="btn btn-sm" :disabled="!hasSlot('src')" @click="pickAll('src')">全用原文</button>
            <button class="btn btn-sm" :disabled="!hasSlot('c0')" @click="pickAll('c0')">全用候选 1</button>
          </footer>
        </div>
        <div v-else-if="results.length" class="results" :class="{ multi: results.length > 1 }">
          <article v-for="r in results" :key="r.variant" class="cand">
            <header class="cand-head">
              <span class="cand-tag">候选 {{ r.variant + 1 }}</span>
              <span v-if="r.model" class="cand-model">{{ shortModel(r.model) }}</span>
              <span class="cand-meta">{{ r.chars }} 字 · {{ (r.elapsed_ms / 1000).toFixed(1) }}s</span>
            </header>
            <!-- 起标题要的是「挑一个」，把五行整块贴回正文没有意义：按行拆开点 -->
            <ul v-if="titleMode" class="titles">
              <li v-for="(line, i) in titleLines(r.text)" :key="i" class="t-item">
                <span class="t-text">{{ line }}</span>
                <span class="t-act">
                  <button class="btn btn-sm" @click="pickTitle(line, 'node')">设为节标题</button>
                  <button class="btn btn-sm" @click="pickTitle(line, 'work')">设为作品标题</button>
                </span>
              </li>
            </ul>
            <pre v-else class="cand-text">{{ r.text }}</pre>
            <footer v-if="!titleMode" class="cand-foot">
              <button class="btn btn-primary btn-sm" @click="adopt(r.text)">
                <AppIcon name="check" :size="13" /> 采用
              </button>
              <button class="btn btn-sm" @click="copy(r.text)">{{ copied === r.variant ? '已复制' : '复制' }}</button>
            </footer>
          </article>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  open: { type: Boolean, default: false },
  text: { type: String, default: '' },
  selection: { type: String, default: '' },
  // 选区前后的正文窗口：随请求发后端，加工时对齐语气称谓（StudioView 组装）
  context: { type: String, default: '' },
  // 底部提示词框里那句本轮要求：带进来做默认值，省得在两个地方抄同一句话
  instruction: { type: String, default: '' },
})
const emit = defineEmits(['update:open', 'adopt'])

const store = useStudioStore()

const actions = [
  { id: 'rewrite', label: '重写（改节奏与表达）' },
  { id: 'polish', label: '润色（只改语言问题）' },
  { id: 'condense', label: '压缩到 40%-60%' },
  { id: 'expand', label: '扩写到 1.8-2.2 倍' },
  { id: 'split', label: '拆成带小标题的段落' },
  { id: 'merge', label: '合并成一段' },
  { id: 'translate', label: '翻译' },
  { id: 'title', label: '起标题（5 个备选）' },
]

const action = ref('rewrite')
const candidates = ref(1)
const instruction = ref('')
const lang = ref('英文')
const text = ref('')
const copied = ref(-1)
// 跨模型对比：至多 3 家，勾选后后端每家各出一条，候选数不生效
const compareModels = ref([])
const mixOn = ref(false)
const mixPick = ref([])

const results = computed(() => store.transformResult?.candidates || [])
const srcChars = computed(() => text.value.length)
const hasSelection = computed(() => props.selection.trim().length > 0)
const titleMode = computed(() => action.value === 'title')
const fullText = computed(() => (props.text || '').slice(0, 20000))

watch(() => props.open, (v) => {
  if (!v) return
  // 起标题看的是整节在讲什么，默认给选区那几行不够；其余动作默认只加工选中那段
  const src = titleMode.value ? props.text : (props.selection || props.text)
  text.value = (src || '').slice(0, 20000)
  instruction.value = props.instruction || ''
  copied.value = -1
  compareModels.value = []
  mixOn.value = false
  store.transformResult = null
})

// 候选换了（重跑一轮）混排视图就过期；切到起标题时混排没意义
watch(results, () => { mixOn.value = false })
watch(titleMode, (v) => { if (v) mixOn.value = false })

const modelChips = computed(() => (store.allGenModels || []).slice(0, 12))

function shortModel(m) {
  // 直连模型名常带供应商前缀（openai/gpt-4o 这种），卡片上只露后半截
  const s = (m || '').split('/').pop()
  return s.length > 22 ? `${s.slice(0, 21)}…` : s
}

function toggleCompare(id) {
  const i = compareModels.value.indexOf(id)
  if (i >= 0) compareModels.value.splice(i, 1)
  else if (compareModels.value.length < 3) compareModels.value.push(id)
}

function useSelection() {
  if (props.selection) text.value = props.selection.slice(0, 20000)
}

function close() {
  emit('update:open', false)
}

async function run() {
  copied.value = -1
  mixOn.value = false
  await store.transform({
    text: text.value,
    action: action.value,
    candidates: candidates.value,
    instruction: instruction.value,
    lang: lang.value,
    context: props.context,
    models: compareModels.value.slice(0, 3),
  })
}

function adopt(t) {
  emit('adopt', t)
  close()
}

// ==================== 逐段混合 ====================

/** 段落切法与写作台生成预览一致：空行或单换行都算分段 */
function splitParas(t) {
  return (t || '').split(/\n\s*\n|\n/).map((s) => s.trim()).filter(Boolean)
}

const srcParas = computed(() => splitParas(text.value))
const candParas = computed(() => results.value.map((r) => splitParas(r.text)))

/**
 * 一行一个「段槽」：候选段落按各自段序对位。
 * 段数不齐是常态（压缩会少段、拆分会多段）：缺的那家就不出现在这一行，
 * 对不齐的尾段也照常成行——宁可让人手动挑，不要静默丢内容。
 */
const mixSlots = computed(() => {
  const total = Math.max(srcParas.value.length, ...candParas.value.map((p) => p.length), 0)
  const slots = []
  for (let i = 0; i < total; i++) {
    const opts = []
    candParas.value.forEach((list, vi) => {
      if (list[i]) {
        const m = results.value[vi]?.model
        opts.push({
          key: `c${vi}`,
          tag: `候选${(results.value[vi]?.variant ?? vi) + 1}${m ? ` · ${shortModel(m)}` : ''}`,
          text: list[i],
        })
      }
    })
    if (srcParas.value[i]) opts.push({ key: 'src', tag: '原文', text: srcParas.value[i] })
    opts.push({ key: 'skip', tag: '丢弃', text: '' })
    slots.push({ opts })
  }
  return slots
})

function pickSlot(i, key) {
  mixPick.value[i] = key
}

function hasSlot(key) {
  return mixSlots.value.some((s) => s.opts.some((o) => o.key === key))
}

function pickAll(key) {
  // 某行没这个选项时退到丢弃？不退——保持该行现选，免得一键把尾段全清了
  mixPick.value = mixSlots.value.map((s, i) => (
    s.opts.some((o) => o.key === key) ? key : (mixPick.value[i] || 'skip')
  ))
}

function toggleMix() {
  mixOn.value = !mixOn.value
  if (!mixOn.value) return
  // 默认候选 1；这一行没有候选 1（它段数少）就退回原文，原文也没有就丢弃
  mixPick.value = mixSlots.value.map((s) => {
    if (s.opts.some((o) => o.key === 'c0')) return 'c0'
    if (s.opts.some((o) => o.key === 'src')) return 'src'
    return 'skip'
  })
}

const mixUsed = computed(() => mixSlots.value.filter((s, i) => {
  const opt = s.opts.find((o) => o.key === mixPick.value[i])
  return opt && opt.key !== 'skip' && opt.text
}).length)

const mixTotal = computed(() => mixSlots.value.length)

const mixComposed = computed(() => {
  const parts = []
  mixSlots.value.forEach((slot, i) => {
    const opt = slot.opts.find((o) => o.key === mixPick.value[i])
    if (opt && opt.key !== 'skip' && opt.text) parts.push(opt.text)
  })
  return parts.join('\n\n')
})

/** 模型爱给「1. 标题」这种带号行，也爱顺手加书名号，回填前都剥掉 */
function titleLines(t) {
  return (t || '')
    .split('\n')
    .map((s) => s.replace(/^\s*(?:[-*]|\d+[.、)])\s*/, '').replace(/[《》]/g, '').trim())
    .filter(Boolean)
    .slice(0, 10)
}

function pickTitle(line, target) {
  emit('adopt', { action: 'title', text: line, target })
  close()
}

async function copy(t) {
  try {
    await navigator.clipboard.writeText(t)
    copied.value = results.value.find((r) => r.text === t)?.variant ?? -1
    setTimeout(() => { copied.value = -1 }, 1500)
  } catch { /* 剪贴板不可用就不提示 */ }
}
</script>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 120;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--overlay);
}
.dlg {
  width: min(920px, 100%);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-stage, 16px);
  background: var(--panel);
  box-shadow: 0 32px 80px -32px rgba(var(--shade-rgb), 0.9);
}
.dlg-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--line-soft);
}
.dlg-head h2 {
  margin: 0 0 4px;
  font-family: theme('fontFamily.serif');
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
}
.dlg-sub {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-faint);
}
.dlg-head .icon-btn {
  margin-left: auto;
}
.dlg-body {
  padding: 14px 18px 18px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.row {
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
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-faint);
}
.cnt {
  font-style: normal;
  font-family: theme('fontFamily.mono');
}
.fl .field {
  width: 190px;
}
.fl .field.narrow {
  width: 110px;
}
.fl.grow {
  flex: 1;
  min-width: 220px;
}
.fl.grow .field {
  width: 100%;
}
.link {
  border: none;
  background: none;
  padding: 0;
  color: var(--run);
  font-size: 11px;
  cursor: pointer;
}
.link:hover {
  text-decoration: underline;
}
.fl > .field.src,
.fl .field.src {
  width: 100%;
  min-height: 120px;
  font-family: theme('fontFamily.serif');
  font-size: 14px;
  line-height: 1.8;
  resize: vertical;
}
.run-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.run-row .btn {
  margin-left: auto;
}
.err {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}
.waiting {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted);
}
.spinner {
  width: 10px;
  height: 10px;
  border: 1.6px solid rgba(var(--run-rgb), 0.3);
  border-top-color: var(--run);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
.dlg-tip {
  margin: 0;
  font-size: 11px;
  color: var(--text-faint);
  line-height: 1.6;
}
/* 起标题的结果：一行一条，按钮贴着右边走，扫一眼就能挑 */
.titles {
  list-style: none;
  margin: 0;
  padding: 4px 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.t-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 12px;
}
.t-text {
  flex: 1;
  min-width: 0;
  font-family: theme('fontFamily.serif');
  font-size: 14px;
  line-height: 1.7;
  color: var(--text);
}
.t-act {
  display: flex;
  flex: none;
  gap: 6px;
}
.results {
  display: grid;
  gap: 10px;
}
.results.multi {
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}
.cand {
  display: flex;
  flex-direction: column;
  border: 1px solid rgba(var(--run-rgb), 0.22);
  border-radius: var(--radius-panel);
  background: rgba(var(--run-rgb), 0.04);
  overflow: hidden;
}
.cand-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 11px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 11px;
}
.cand-tag {
  color: var(--run);
  font-weight: 600;
}
.cand-meta {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  color: var(--text-faint);
}
.cand-text {
  margin: 0;
  padding: 10px 12px;
  max-height: 34vh;
  overflow-y: auto;
  font-family: theme('fontFamily.serif');
  font-size: 13.5px;
  line-height: 1.85;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.cand-foot {
  display: flex;
  gap: 8px;
  padding: 8px 11px;
  border-top: 1px solid var(--line-soft);
}
@media (prefers-reduced-motion: reduce) {
  .spinner { animation-duration: 3s; }
}
/* ---------- 跨模型对比 ---------- */
.models {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.models-label {
  font-size: 11px;
  color: var(--text-faint);
}
.models-label em {
  font-style: normal;
  margin-left: 6px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 96px;
  overflow-y: auto;
}
.chip {
  padding: 3px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: transparent;
  font-size: 11.5px;
  color: var(--text-muted);
  cursor: pointer;
}
.chip:hover {
  border-color: rgba(var(--run-rgb), 0.45);
  color: var(--text);
}
.chip.on {
  border-color: var(--run);
  color: var(--run-fg);
  background: rgba(var(--run-rgb), 0.08);
}
/* ---------- 逐段混合 ---------- */
.res-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.res-hint {
  font-size: 11px;
  color: var(--text-faint);
}
.mix {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.mix-slot {
  padding: 8px 10px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-panel);
}
.mix-no {
  margin: 0 0 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-faint);
}
.mix-opts {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.mix-opt {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.mix-opt:hover {
  border-color: rgba(var(--run-rgb), 0.45);
}
.mix-opt.on {
  border-color: var(--run);
  background: rgba(var(--run-rgb), 0.06);
}
.mix-tag {
  flex: none;
  font-size: 10.5px;
  color: var(--text-faint);
}
.mix-opt.on .mix-tag {
  color: var(--run);
  font-weight: 600;
}
.mix-text {
  font-family: theme('fontFamily.serif');
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
  /* 未选中的行折到四行，整屏才装得下段槽；选中那坨放开看全文 */
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 4;
  overflow: hidden;
}
.mix-opt.on .mix-text {
  display: block;
  -webkit-line-clamp: unset;
  overflow: visible;
}
.mix-foot {
  display: flex;
  align-items: center;
  gap: 8px;
}
.cand-model {
  padding: 1px 7px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 10.5px;
  color: var(--text-muted);
}
</style>
