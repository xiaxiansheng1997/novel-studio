<template>
  <div v-if="open" class="mask" @click.self="close">
    <div class="box" role="dialog" aria-modal="true" aria-label="导入已有稿件">
      <header class="head">
        <AppIcon name="download" :size="15" />
        <span class="head-title">导入已有稿件</span>
        <button class="esc" title="关闭（Esc）" @click="close">
          <span>Esc</span>
          <AppIcon name="x" :size="12" />
        </button>
      </header>

      <div class="body">
        <p class="tip">
          粘一篇旧稿，或把 .txt / .md 拖进来。导进来的每一节都是正常正文：能接着写、能被检索、算进作品字数；
          但它不算「今天写的字」，写作日历不会被旧稿刷高。
          <template v-if="parentTitle">这次会挂在「{{ parentTitle }}」下面。</template>
        </p>

        <label class="drop" @dragover.prevent @drop.prevent="onDrop">
          <input type="file" accept=".txt,.md,.markdown,.text" hidden @change="onPick" />
          <span>{{ fileName || '拖入 .txt / .md，或点这里选文件' }}</span>
        </label>

        <textarea
          v-model="text"
          class="field area"
          rows="9"
          placeholder="…也可以直接把正文粘在这里"
        ></textarea>

        <div class="row">
          <label class="fl">
            <span>怎么拆节</span>
            <select v-model="mode" class="field">
              <option value="auto">自动（有「第X章」就按标题拆）</option>
              <option value="heading">按「第X章 / 第X节 / ## 标题」拆</option>
              <option value="blank">按空行拆</option>
              <option value="single">整篇算一节</option>
            </select>
          </label>
          <label class="fl">
            <span>整篇算一节时的标题</span>
            <input v-model="singleTitle" class="field" placeholder="留空取文件名或「导入的稿件」" />
          </label>
        </div>

        <p class="stat">
          将导入 <strong>{{ sections.length }}</strong> 节 · {{ totalChars }} 字
          <template v-if="!sections.length"> —— 上面还没有可导入的文字</template>
        </p>

        <ol v-if="sections.length" class="preview">
          <li v-for="(s, i) in previewList" :key="i">
            <span class="pv-title">{{ s.title }}</span>
            <span class="pv-chars">{{ s.content.length }}</span>
            <span class="pv-first">{{ firstLine(s.content) }}</span>
          </li>
          <li v-if="sections.length > previewList.length" class="pv-more">
            还有 {{ sections.length - previewList.length }} 节没展开
          </li>
        </ol>

        <p v-if="error" class="warn-line">{{ error }}</p>
      </div>

      <footer class="foot">
        <span v-if="busy" class="prog">正在导入 {{ progress }} / {{ sections.length }}…</span>
        <span v-else class="foot-tip">拆错了换个方式再看，点「导入」之前一个字都不会写库</span>
        <div class="foot-act">
          <button class="btn btn-sm" :disabled="busy" @click="close">取消</button>
          <button
            class="btn btn-primary btn-sm"
            :disabled="busy || !sections.length"
            @click="run"
          >{{ busy ? '导入中…' : `导入 ${sections.length} 节` }}</button>
        </div>
      </footer>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  open: { type: Boolean, default: false },
  // 导入到哪儿：当前选中的是卷（容器）就挂进去，否则落在根上
  parentId: { type: String, default: '' },
  parentTitle: { type: String, default: '' },
  // 小说落「章」、单篇落「节」，与新建节点同一套推断
  leafKind: { type: String, default: 'chapter' },
})
const emit = defineEmits(['update:open', 'done'])
const store = useStudioStore()

const text = ref('')
const fileName = ref('')
const singleTitle = ref('')
const mode = ref('auto')
const busy = ref(false)
const progress = ref(0)
const error = ref('')

// 「第 3 章」「第三章」「## 二、」这类都当标题；行长超过 40 字的当正文，
// 免得把正文里一句「第一章里他……」误判成分节点
const HEAD_RE = /^\s*(?:#{1,6}\s+\S|第\s*[0-9零一二三四五六七八九十百千两]+\s*[章节回卷篇部]|Chapter\s+\d+\b)/i

function cleanTitle(line) {
  return line.replace(/^\s*#{1,6}\s*/, '').trim().slice(0, 60)
}

function splitByHeading(raw) {
  const out = []
  let cur = null
  for (const line of raw.split('\n')) {
    if (HEAD_RE.test(line) && line.trim().length <= 40) {
      if (cur) out.push(cur)
      cur = { title: cleanTitle(line), body: [] }
    } else {
      if (!cur) cur = { title: '', body: [] }
      cur.body.push(line)
    }
  }
  if (cur) out.push(cur)
  return out
    .map((s, i) => ({ title: s.title || `第 ${i + 1} 节`, content: s.body.join('\n').trim() }))
    .filter((s) => s.content)
}

function splitByBlank(raw) {
  return raw
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean)
    .map((body, i) => {
      const first = body.split('\n')[0].trim()
      return { title: first.slice(0, 24) || `第 ${i + 1} 节`, content: body }
    })
}

function fallbackTitle() {
  const named = singleTitle.value.trim()
  if (named) return named
  const fromFile = fileName.value.replace(/\.[^.]+$/, '').trim()
  return fromFile.slice(0, 60) || '导入的稿件'
}

const sections = computed(() => {
  const raw = (text.value || '').replace(/\r\n/g, '\n').trim()
  if (!raw) return []
  if (mode.value === 'single') return [{ title: fallbackTitle(), content: raw }]
  if (mode.value === 'heading') return splitByHeading(raw)
  if (mode.value === 'blank') return splitByBlank(raw)
  // 自动：标题拆得出两节以上就信它，否则退到空行，再否则整篇
  const byHead = splitByHeading(raw)
  if (byHead.length >= 2) return byHead
  const byBlank = splitByBlank(raw)
  return byBlank.length >= 2 ? byBlank : [{ title: fallbackTitle(), content: raw }]
})

const totalChars = computed(() => sections.value.reduce((n, s) => n + s.content.length, 0))
const previewList = computed(() => sections.value.slice(0, 8))

function firstLine(body) {
  return (body.split('\n').find((l) => l.trim()) || '').trim().slice(0, 40)
}

async function readFile(file) {
  if (!file) return
  if (!/\.(txt|md|markdown|text)$/i.test(file.name)) {
    error.value = '只支持 .txt / .md；Word 与 PDF 请先另存为纯文本'
    return
  }
  try {
    text.value = await file.text()
    fileName.value = file.name
    error.value = ''
  } catch (e) {
    error.value = `读不了这个文件：${String(e?.message || e)}`
  }
}

async function onPick(e) {
  await readFile(Array.from(e.target.files || [])[0])
  e.target.value = ''
}

async function onDrop(e) {
  await readFile(Array.from(e.dataTransfer?.files || [])[0])
}

async function run() {
  const list = sections.value
  if (!list.length || busy.value) return
  busy.value = true
  progress.value = 0
  error.value = ''
  try {
    const res = await store.importNodes(
      list,
      { kind: props.leafKind || 'chapter', parentId: props.parentId || null },
      (done) => { progress.value = done },
    )
    if (res.created && !res.failed) {
      emit('done', res)
      close()
      return
    }
    error.value = res.created
      ? `导进去 ${res.created} 节，${res.failed} 节没成功（标题为空或超长会被后端拒掉）`
      : '一节都没导进去：确认服务还在跑，再换一段文字试试'
  } finally {
    busy.value = false
  }
}

function close() {
  if (busy.value) return
  emit('update:open', false)
}

function onKey(e) {
  if (e.key === 'Escape' && props.open && !busy.value) {
    e.preventDefault()
    close()
  }
}

watch(() => props.open, (on) => {
  if (!on) return
  text.value = ''
  fileName.value = ''
  singleTitle.value = ''
  mode.value = 'auto'
  error.value = ''
  progress.value = 0
  busy.value = false
})

onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 140;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(5, 6, 8, 0.66);
}
.box {
  width: min(760px, 100%);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-stage, 16px);
  background: var(--panel);
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.5);
  overflow: hidden;
}
.head {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-faint);
}
.head-title {
  font-size: 13px;
  color: var(--text);
}
.esc {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  height: 24px;
  padding: 0 7px;
  border: 1px solid var(--line-soft);
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  font-size: 10px;
  font-family: theme('fontFamily.mono');
  cursor: pointer;
}
.esc:hover {
  color: var(--text);
  border-color: var(--line);
}
.body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tip {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-faint);
}
.drop {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 46px;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  color: var(--text-faint);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s var(--ease), color 0.15s var(--ease);
}
.drop:hover {
  border-color: var(--run);
  color: var(--text-muted);
}
.area {
  min-height: 150px;
  resize: vertical;
}
.row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.fl {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11px;
  color: var(--text-faint);
}
.stat {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
}
.stat strong {
  color: var(--run);
  font-family: theme('fontFamily.mono');
}
.preview {
  margin: 0;
  padding: 0;
  list-style: none;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  overflow: hidden;
}
.preview li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 6px 9px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12px;
}
.preview li:last-child {
  border-bottom: none;
}
.pv-title {
  flex-shrink: 0;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.pv-chars {
  flex-shrink: 0;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
}
.pv-first {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-faint);
}
.pv-more {
  color: var(--text-faint);
  justify-content: center;
}
.warn-line {
  margin: 0;
  font-size: 12px;
  color: #fca5a5;
}
.foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-top: 1px solid var(--line-soft);
}
.foot-tip,
.prog {
  font-size: 11.5px;
  color: var(--text-faint);
}
.prog {
  color: var(--text-muted);
}
.foot-act {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
</style>
