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
          粘一篇旧稿，或把 .txt / .md / .docx / .epub 拖进来。导进来的每一节都是正常正文：能接着写、能被检索、算进作品字数；
          但它不算「今天写的字」，写作日历不会被旧稿刷高。
          <template v-if="parentTitle">这次会挂在「{{ parentTitle }}」下面。</template>
        </p>

        <label class="drop" @dragover.prevent @drop.prevent="onDrop">
          <input type="file" accept=".txt,.md,.markdown,.text,.mdown,.mkd,.docx,.epub" hidden @change="onPick" />
          <span>{{ fileName || '拖入 .txt / .md / .docx / .epub，或点这里选文件' }}</span>
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
              <option value="auto">自动（认得章节标题；整本有卷就分层）</option>
              <option value="level">按标题层级（一级标题当卷，二级当章）</option>
              <option value="heading">按「第X章 / 第X节 / ## 标题」拆（全部平铺）</option>
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
          将导入
          <template v-if="volumeCount"> <strong>{{ volumeCount }}</strong> 卷 ·</template>
          <strong>{{ leafCount }}</strong> 节 · {{ totalChars }} 字
          <template v-if="!leafCount"> —— 上面还没有可导入的文字</template>
        </p>

        <ol v-if="wantNesting" class="preview">
          <li v-for="(r, i) in previewRows" :key="i">
            <span class="pv-title" :class="{ 'is-vol': r.isVol }">{{ r.title }}</span>
            <span class="pv-chars">{{ r.count }}</span>
            <span class="pv-first">{{ r.first }}</span>
          </li>
          <li v-if="nestable.length > previewRows.length" class="pv-more">
            还有 {{ nestable.length - previewRows.length }} 项没展开
          </li>
        </ol>

        <ol v-else-if="sections.length" class="preview">
          <li v-for="(s, i) in previewList" :key="i">
            <span class="pv-title">{{ s.title }}</span>
            <span class="pv-chars">{{ s.content.length }}</span>
            <span class="pv-first">{{ firstLine(s.content) }}</span>
          </li>
          <li v-if="sections.length > previewList.length" class="pv-more">
            还有 {{ sections.length - previewList.length }} 节没展开
          </li>
        </ol>

        <p v-if="notice" class="note-line">{{ notice }}</p>
        <p v-if="error" class="warn-line">{{ error }}</p>
      </div>

      <footer class="foot">
        <span v-if="busy" class="prog">{{ parseHint }}</span>
        <span v-else class="foot-tip">拆错了换个方式再看，点「导入」之前一个字都不会写库</span>
        <div class="foot-act">
          <button class="btn btn-sm" :disabled="busy" @click="close">取消</button>
          <button
            class="btn btn-primary btn-sm"
            :disabled="busy || !leafCount"
            @click="run"
          >{{ busy ? '导入中…' : `导入 ${leafCount} 节` }}</button>
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
const notice = ref('')
const parsing = ref(false)
const parsedKind = ref('')

// 「第 3 章」「第三章」「## 二、」这类都当标题；行长超过 40 字的当正文，
// 免得把正文里一句「第一章里他……」误判成分节点
const HEAD_RE = /^\s*(?:#{1,6}\s+\S|第\s*[0-9零一二三四五六七八九十百千两]+\s*[章节回卷篇部]|Chapter\s+\d+\b)/i

// 能交给后端解的类型；PDF / 老 doc 不在这里（解不了，但要说清下一步）
const PARSER_EXTS = /\.(txt|md|markdown|text|mdown|mkd|docx|epub)$/i
const NEED_CONVERT = /\.(pdf|doc)$/i

function cleanTitle(line) {
  return line.replace(/^\s*#{1,6}\s*/, '').trim().slice(0, 60)
}

// 标题级数：先看标题写的是什么，再看井号数。
// 「# 第一章」这种很常见（markdown 里每章一个 #），按井号算会拆出两百个「卷」；
// 反过来「# 第一卷 / # 第一章」混着用的书，靠关键词才能分出两层。
// 后端解 epub/docx 时已经把 h1→#、h2→## 写进文本，这里是唯一一处读它的地方。
function headingLevel(line) {
  const hashes = /^\s*(#{1,6})\s+\S/.exec(line)
  const bare = line.replace(/^\s*#+\s*/, '')
  if (/^第\s*[0-9零一二三四五六七八九十百千两]+\s*[卷篇部集]/.test(bare)) return 1
  if (/^第\s*[0-9零一二三四五六七八九十百千两]+\s*[章回节]/.test(bare)) return 2
  if (/^part\s+([0-9]+|[ivxlcdm]+)\b/i.test(bare)) return 1
  if (/^chapter\s+\d+/i.test(bare)) return 2
  if (hashes) return Math.min(hashes[1].length, 2)
  return 2
}

function splitByHeading(raw) {
  const out = []
  let cur = null
  for (const line of raw.split('\n')) {
    if (HEAD_RE.test(line) && line.trim().length <= 40) {
      if (cur) out.push(cur)
      cur = { title: cleanTitle(line), level: headingLevel(line), body: [] }
    } else {
      if (!cur) cur = { title: '', level: 1, body: [] }
      cur.body.push(line)
    }
  }
  if (cur) out.push(cur)
  return out
    .map((s, i) => ({
      title: s.title || `第 ${i + 1} 节`,
      level: s.level || 1,
      content: s.body.join('\n').trim(),
    }))
    // 一级标题就算没正文也可能是一个「卷」，先留着；二级以下的空节没东西可导
    .filter((s) => s.content || s.level <= 1)
}

function splitByBlank(raw) {
  return raw
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean)
    .map((body, i) => {
      const first = body.split('\n')[0].trim()
      return { title: first.slice(0, 24) || `第 ${i + 1} 节`, level: 1, content: body }
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
  if (mode.value === 'single') return [{ title: fallbackTitle(), level: 1, content: raw }]
  if (mode.value === 'heading' || mode.value === 'level') return splitByHeading(raw)
  if (mode.value === 'blank') return splitByBlank(raw)
  // 自动：标题拆得出两节以上就信它，否则退到空行，再否则整篇
  const byHead = splitByHeading(raw)
  if (byHead.length >= 2) return byHead
  const byBlank = splitByBlank(raw)
  return byBlank.length >= 2 ? byBlank : [{ title: fallbackTitle(), level: 1, content: raw }]
})

// 平铺导入不认空正文的标题（那只是一个「卷」的占位）
const flatSections = computed(() => sections.value.filter((s) => s.content))

// 两层大纲：一级标题当卷、其下二级标题挂在卷下。头部先出现二级、或一级标题
// 后面又跟一级时都不会串——没有卷可挂的落在根上；根上的独立章攒成一批发。
function buildOutline(list) {
  const roots = []
  let vol = null
  for (const s of list) {
    const node = { title: s.title, level: s.level || 1, content: s.content, children: [] }
    if (node.level <= 1) {
      vol = node
      roots.push(node)
    } else if (vol) {
      vol.children.push(node)
    } else {
      roots.push(node)
    }
  }
  return roots
}

const outline = computed(() => buildOutline(sections.value))

// 卷首语不能丢：一级标题自带正文的，补成这一卷的第一节（不然那几段话无处可去）
const nestable = computed(() => outline.value
  .map((v) => (v.children.length && v.content.trim()
    ? {
        ...v,
        content: '',
        children: [
          { title: `${v.title}（卷首）`, level: 2, content: v.content, children: [] },
          ...v.children,
        ],
      }
    : v))
  .filter((v) => v.children.length || v.content.trim()))

const volumes = computed(() => nestable.value.filter((v) => v.children.length))

// 什么时候真按卷导：
//  - 挂在一卷下面导：当然不能又建卷，平铺；
//  - 用户选了「按标题层级」：不猜，直接分层；
//  - 自动：只有「卷标题自己没正文、而且真不止一卷」才分层。
//    一本每章都写成 `# 第一章` 的 md，按井号分层会拆出两百个「卷」。
const wantNesting = computed(() => {
  if (props.parentId || !volumes.value.length) return false
  if (mode.value === 'level') return true
  if (mode.value !== 'auto') return false
  return volumes.value.length >= 2
    && outline.value.filter((v) => v.children.length).every((v) => !v.content.trim())
})

const leafCount = computed(() => (wantNesting.value
  ? nestable.value.reduce((n, v) => n + (v.children.length || 1), 0)
  : flatSections.value.length))
const volumeCount = computed(() => (wantNesting.value ? volumes.value.length : 0))

const totalChars = computed(() => flatSections.value.reduce((n, s) => n + s.content.length, 0))
const previewList = computed(() => flatSections.value.slice(0, 8))
// 预览只展前八项。分层模式下卷和「根上的独立章」会混在一起，
// 把后者也画成一卷会让人以为书里多了一层结构
const previewRows = computed(() => nestable.value.slice(0, 8).map((v) => (v.children.length
  ? { title: v.title, isVol: true, count: `${v.children.length} 节`, first: v.children[0]?.title || '' }
  : { title: v.title, isVol: false, count: `${v.content.length} 字`, first: firstLine(v.content) })))

// 底栏那句进度：解析、导入、建索引是三件事，得分开说。
// 一百多万字的索引要跑二十秒（实测），而那段时间里节已经全部落库了：
// 不说「在建索引」，人只会以为卡在 2000/2000 断掉了。
const parseHint = computed(() => {
  if (parsing.value) return `正在解析 ${parsedKind.value}…`
  if (store.indexing) return '节都进去了，正在给正文建索引（检索与拆解要用）：书越长这一步越久…'
  return `正在导入 ${progress.value} / ${leafCount.value}…`
})

function firstLine(body) {
  return (body.split('\n').find((l) => l.trim()) || '').trim().slice(0, 40)
}

async function readFile(file) {
  if (!file) return
  notice.value = ''
  if (!PARSER_EXTS.test(file.name)) {
    error.value = NEED_CONVERT.test(file.name)
      ? '这种格式解不了：PDF 请先用 Calibre / Word 另存为 .epub 或 .docx，老 .doc 也请先另存为 .docx'
      : '只支持 .txt / .md / .docx / .epub'
    return
  }
  // 文本类也交给后端：浏览器 file.text() 只按 UTF-8 硬读，
  // Windows 上发过来的 GBK 中文稿会变成一路不报错的乱码，等导进库才发现整本废了
  busy.value = true
  parsing.value = true
  parsedKind.value = (file.name.match(/\.([^.]+)$/) || ['', '文件'])[1].toLowerCase()
  try {
    const r = await store.fileToText(file)
    if (!r.ok) {
      error.value = r.reason || '这个文件解不开'
      return
    }
    text.value = r.text
    fileName.value = file.name
    // epub 自带书名，比文件名靠谱；但用户已经手填过就不能覆盖
    if (r.title && !singleTitle.value.trim()) singleTitle.value = r.title
    const bits = []
    if (r.title) bits.push(`书名「${r.title}」`)
    bits.push(
      r.headings
        ? `解出 ${r.chars} 字、${r.headings} 行标题：默认「自动」就能按标题拆`
        : '解出纯文本了，但没认出标题行：拆不开就换下面的拆法',
    )
    if (r.encoding && r.encoding !== 'UTF-8' && r.encoding !== 'UTF-8 (BOM)') {
      bits.push(
        r.encoding === 'unknown'
          ? '编码没认出来，是按 UTF-8 硬解的——预览里若是乱码，请把文件另存为 UTF-8 再导'
          : `不是 UTF-8，已按 ${r.encoding} 解`,
      )
    }
    error.value = ''
    if (r.chars > 200000) bits.push('稿子很长，下面预览会慢半拍')
    notice.value = bits.join(' · ')
  } finally {
    busy.value = false
    parsing.value = false
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
  const nesting = wantNesting.value
  const list = nesting ? nestable.value : flatSections.value
  if (!list.length || busy.value) return
  busy.value = true
  progress.value = 0
  error.value = ''
  const kind = props.leafKind || 'chapter'
  const tick = (done) => { progress.value = done }
  try {
    // 分层导入走 importOutline（一级标题建卷），平铺走 importNodes；
    // 两条路都按节报进度，分层的总数是卷下章数，不含卷
    const res = nesting
      ? await store.importOutline(list, { kind }, tick)
      : await store.importNodes(list, { kind, parentId: props.parentId || null }, tick)
    if (res.created && !res.failed) {
      emit('done', res)
      // 导完了必须关窗：close() 里的「busy 不许关」是给遮罩/Esc/取消设的，
      // 不是给这条成功路径设的。不先把 busy 放下来就关不掉，窗子留在屏幕上
      // 带着同一份正文，再点一次就是重复导一批节
      busy.value = false
      close()
      return
    }
    // 哪几节没进来、为什么没进来，后端逐条报了名；只展前三条，
    // 剩下的报个数——把几百条原因堆在弹层里等于没说
    const why = (res.reasons || []).slice(0, 3).join('；')
    const more = res.reasons && res.reasons.length > 3 ? `等 ${res.reasons.length} 条` : ''
    const tail = res.stopped ? '（已经停在中间，后面的节没发）' : ''
    error.value = [
      res.created
        ? `导进去 ${res.created} 节${res.volumes ? `（${res.volumes} 卷）` : ''}，${res.failed} 节没成功${why ? `：${why}${more}` : ''}`
        : `一节都没导进去：${why || '确认服务还在跑，再换一段文字试试'}`,
      tail,
    ].filter(Boolean).join(' ')
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
  notice.value = ''
  progress.value = 0
  busy.value = false
  parsing.value = false
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
  background: var(--overlay);
}
.box {
  width: min(760px, 100%);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-stage, 16px);
  background: var(--panel);
  box-shadow: 0 30px 80px rgba(var(--shade-rgb), 0.5);
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
/* 卷这一行要能一眼看出来是分组，不是又一节 */
.pv-title.is-vol {
  font-weight: 600;
  letter-spacing: 0.2px;
}
.pv-title.is-vol::before {
  content: '卷';
  margin-right: 5px;
  padding: 0 3px;
  border: 1px solid var(--line-strong);
  border-radius: 3px;
  color: var(--text-faint);
  font-size: 9.5px;
  font-weight: 400;
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
  color: var(--danger-fg);
}
.note-line {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--info-fg);
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
