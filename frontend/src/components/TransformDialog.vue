<template>
  <div v-if="open" class="mask" @click.self="close">
    <section class="dlg" role="dialog" aria-modal="true" aria-label="加工选中文字">
      <header class="dlg-head">
        <div>
          <h2>加工</h2>
          <p class="dlg-sub">
            把一段文字交给模型改写；多个候选横向比，选一个写回正文。
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
            <select v-model.number="candidates" class="field narrow">
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
          正在生成 {{ candidates }} 个候选，单个约 5-20 秒
        </div>

        <p v-if="titleMode" class="dlg-tip">点一行就能回填；左边进本节标题，右边进作品标题（导出时那一句 `# 标题`）。</p>
        <div v-if="results.length" class="results" :class="{ multi: results.length > 1 }">
          <article v-for="r in results" :key="r.variant" class="cand">
            <header class="cand-head">
              <span class="cand-tag">候选 {{ r.variant + 1 }}</span>
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
  store.transformResult = null
})

function useSelection() {
  if (props.selection) text.value = props.selection.slice(0, 20000)
}

function close() {
  emit('update:open', false)
}

async function run() {
  copied.value = -1
  await store.transform({
    text: text.value,
    action: action.value,
    candidates: candidates.value,
    instruction: instruction.value,
    lang: lang.value,
  })
}

function adopt(t) {
  emit('adopt', t)
  close()
}

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
  background: rgba(5, 6, 8, 0.66);
}
.dlg {
  width: min(920px, 100%);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-stage, 16px);
  background: var(--panel);
  box-shadow: 0 32px 80px -32px rgba(0, 0, 0, 0.9);
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
  border: 1.6px solid rgba(217, 164, 91, 0.3);
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
  border: 1px solid rgba(217, 164, 91, 0.22);
  border-radius: var(--radius-panel);
  background: rgba(217, 164, 91, 0.04);
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
</style>
