<template>
  <div v-if="open" class="mask" @click.self="close">
    <div class="dlg" role="dialog" aria-modal="true" aria-label="公众号版">
      <header class="dlg-head">
        <div class="head-main">
          <h2>公众号版</h2>
          <p class="dlg-sub">
            <template v-if="stats">
              {{ stats.sections }} 节 · {{ stats.chars }} 字 · 约 {{ stats.read_minutes }} 分钟
            </template>
            <template v-else>正在装配…</template>
          </p>
        </div>
        <button class="x" aria-label="关闭" @click="close">
          <AppIcon name="x" :size="15" />
        </button>
      </header>

      <div class="dlg-body">
        <!-- 预览里就是真正的导出结果：内联样式、白底，跟公众号后台看到的一致 -->
        <div class="phone">
          <div class="phone-bar">手机预览 · 375px</div>
          <div ref="screenEl" class="phone-screen" v-html="html"></div>
        </div>

        <aside class="side">
          <section class="blk">
            <h3>标题</h3>
            <p class="txt strong">{{ title || '（还没有标题）' }}</p>
            <p class="hint">
              {{ title.length }} 字
              <template v-if="title.length > 20">
                · 列表页只露前 20 字：{{ title.slice(0, 20) }}…
              </template>
              <template v-else>· 列表页一屏放得下</template>
            </p>
            <button class="btn btn-sm" :disabled="titlesBusy || !hasBody" @click="makeTitles">
              {{ titlesBusy ? '生成中…' : '换 6 个标题' }}
            </button>
            <ul v-if="titles.length" class="picks">
              <li v-for="(t, i) in titles" :key="i">
                <span class="pick-text">{{ t.text }}</span>
                <span class="pick-n">{{ t.chars }} 字</span>
                <button class="mini" @click="useTitle(t)">用这个</button>
              </li>
            </ul>
          </section>

          <section class="blk">
            <h3>摘要</h3>
            <p v-if="digest" class="txt">{{ digest }}</p>
            <p v-else class="warn">没填摘要：公众号会截正文前 54 字，常常截在半个句子上。</p>
            <button class="btn btn-sm" :disabled="digestBusy || !hasBody" @click="makeDigest">
              {{ digestBusy ? '生成中…' : (digest ? '重写摘要（AI）' : 'AI 写摘要') }}
            </button>
            <p v-if="digestNote" class="hint">{{ digestNote }}</p>
          </section>

          <section class="blk">
            <h3>转发语</h3>
            <p class="hint">发朋友圈、群里是另一套话，跟摘要不是一回事。</p>
            <button class="btn btn-sm" :disabled="shareBusy || !hasBody" @click="makeShare">
              {{ shareBusy ? '生成中…' : '写 3 条' }}
            </button>
            <ul v-if="copies.length" class="picks">
              <li v-for="(c, i) in copies" :key="i">
                <span class="pick-text"><span class="c-angle">{{ c.angle }}</span>{{ c.text }}</span>
                <button class="mini" @click="copyText(c.text)">复制</button>
              </li>
            </ul>
          </section>

          <section class="blk">
            <h3>
              发布前检查
              <span v-if="warnCount" class="badge">{{ warnCount }}</span>
            </h3>
            <p v-if="!items.length" class="txt">没有发现问题。</p>
            <ul v-else class="checks">
              <li v-for="(it, i) in items" :key="i" :class="it.severity">
                <span class="msg">{{ it.message }}</span>
                <button v-if="it.node_id" class="mini" @click="goto(it)">定位</button>
              </li>
            </ul>
          </section>

          <section class="blk">
            <h3>配图清单</h3>
            <p v-if="!images.length" class="txt">
              正文里写「[图：画面描述]」就会列在这里。图片要在公众号后台上传，外链图会被屏蔽。
            </p>
            <ol v-else class="imgs">
              <li v-for="(im, i) in images" :key="i">
                <span class="sec">{{ im.section }}</span>
                <span class="desc">{{ im.desc }}</span>
              </li>
            </ol>
          </section>

          <section class="blk">
            <h3>版式</h3>
            <div class="chips">
              <button
                v-for="(label, id) in themes"
                :key="id"
                class="chip"
                :class="{ on: theme === id }"
                @click="pickTheme(id)"
              >{{ label }}</button>
            </div>
            <p class="hint">只改排版不改正文；选中的会记成默认。</p>
          </section>

          <section class="blk">
            <h3>文末</h3>
            <textarea
              v-model="footerDraft"
              class="field"
              rows="2"
              placeholder="每篇都一样的关注引导，例如「觉得有用，点个关注」"
            ></textarea>
            <label class="row">
              <input v-model="archiveOn" type="checkbox" />
              <span>追加「往期推荐」（取发布台账里带链接的文章）</span>
            </label>
            <button class="btn btn-sm" :disabled="footBusy" @click="saveFooter">
              {{ footBusy ? '保存中…' : '保存并刷新' }}
            </button>
          </section>
        </aside>
      </div>

      <footer class="dlg-foot">
        <span class="tip">{{ note || '复制后到公众号编辑器里粘贴，格式会跟着过来' }}</span>
        <button class="btn btn-sm" :disabled="!plain" @click="copyPlain">复制纯文本</button>
        <button class="btn btn-primary btn-sm" :disabled="!html" @click="copyRich">
          <AppIcon name="copy" :size="13" /> 复制到公众号
        </button>
      </footer>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['update:open', 'goto'])
const store = useStudioStore()

const html = ref('')
const plain = ref('')
const chk = ref(null)
const digest = ref('')
const digestBusy = ref(false)
const digestNote = ref('')
const note = ref('')
const screenEl = ref(null)

// 标题与转发语都是「生成候选 → 点一下才落地」，不自动改作品
const title = ref('')
const titles = ref([])
const titlesBusy = ref(false)
const copies = ref([])
const shareBusy = ref(false)
// 版式与文末存在全局设置里：一个号的排版是稳定的，不该每篇重设一遍
const theme = ref('paper')
const themes = ref({})
const footerDraft = ref('')
const archiveOn = ref(false)
const footBusy = ref(false)

const items = computed(() => chk.value?.items || [])
const stats = computed(() => chk.value?.stats || null)
const images = computed(() => chk.value?.images || [])
const warnCount = computed(() => items.value.filter((i) => i.severity === 'warn').length)
const hasBody = computed(() => (stats.value?.chars || 0) > 0)

function close() {
  emit('update:open', false)
}

/** 富文本之外再备一份纯文本：粘到不支持 HTML 的地方时不至于什么都没有 */
function plainOf(markup) {
  if (!markup) return ''
  const el = document.createElement('div')
  el.innerHTML = markup.replace(/<\/(p|h1|h2|li|blockquote|pre|section)>/g, '\n')
  return (el.textContent || '').replace(/\n{3,}/g, '\n\n').trim()
}

async function load() {
  note.value = ''
  digestNote.value = ''
  // 先拿体检结果：版式默认值、文末、标题都在里面，拿到之后再渲染 HTML，
  // 否则会先用 paper 渲一遍再被设置覆盖，预览闪一下
  const check = await store.publishCheck()
  chk.value = check
  title.value = check?.title || ''
  theme.value = check?.theme || 'paper'
  themes.value = check?.themes || {}
  footerDraft.value = check?.footer || ''
  archiveOn.value = Boolean(check?.archive_on)
  digest.value = check?.digest || ''
  await reloadHtml()
}

async function reloadHtml() {
  const page = await store.exportHtml({ theme: theme.value })
  html.value = page || ''
  plain.value = plainOf(html.value)
}

async function pickTheme(id) {
  theme.value = id
  // 存成默认：下次打开、下一次导出都是这个版式
  await store.saveSetting('theme', id)
  await reloadHtml()
}

async function saveFooter() {
  footBusy.value = true
  try {
    await store.saveSetting('footer_text', footerDraft.value.trim())
    await store.saveSetting('footer_archive', archiveOn.value ? '1' : '0')
    await reloadHtml()
    note.value = '文末已保存'
  } finally {
    footBusy.value = false
  }
}

async function makeTitles() {
  titlesBusy.value = true
  titles.value = []
  try {
    const got = await store.generateTitles({ count: 6 })
    titles.value = got
    if (!got.length) note.value = store.error || '没拿到候选，重试一次'
  } finally {
    titlesBusy.value = false
  }
}

async function useTitle(t) {
  if (!(await store.updateWork({ title: t.text }))) {
    note.value = store.error || '改标题失败'
    return
  }
  title.value = t.text
  titles.value = []
  note.value = '标题已换，记得回编辑器看一眼标题和正文是否还对得上'
}

async function makeShare() {
  shareBusy.value = true
  try {
    copies.value = await store.shareCopies({ count: 3 })
    if (!copies.value.length) note.value = store.error || '没拿到转发语，重试一次'
  } finally {
    shareBusy.value = false
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    note.value = '转发语已复制'
  } catch {
    note.value = '剪贴板不可用，手动选中复制'
  }
}

watch(() => props.open, (v) => {
  if (v) load()
})

async function copyRich() {
  if (!html.value) return
  try {
    if (!navigator.clipboard?.write || !window.ClipboardItem) {
      throw new Error('这个浏览器不支持富文本剪贴板')
    }
    await navigator.clipboard.write([new ClipboardItem({
      'text/html': new Blob([html.value], { type: 'text/html' }),
      'text/plain': new Blob([plain.value], { type: 'text/plain' }),
    })])
    note.value = '已复制，去公众号编辑器粘贴'
  } catch (e) {
    // 退化：选中预览区再走老命令，多数浏览器仍能带上格式
    try {
      const el = screenEl.value
      if (!el) throw e
      const range = document.createRange()
      range.selectNodeContents(el)
      const sel = window.getSelection()
      sel.removeAllRanges()
      sel.addRange(range)
      const ok = document.execCommand('copy')
      note.value = ok ? '已复制（兼容模式）' : `复制失败：${String(e?.message || e)}`
    } catch (e2) {
      note.value = `复制失败：${String(e2?.message || e2)}`
    }
  }
}

async function copyPlain() {
  try {
    await navigator.clipboard.writeText(plain.value)
    note.value = '纯文本已复制'
  } catch (e) {
    note.value = `复制失败：${String(e?.message || e)}`
  }
}

async function makeDigest() {
  digestBusy.value = true
  digestNote.value = ''
  try {
    const got = await store.writeDigest()
    if (!got) {
      digestNote.value = store.error || '写摘要失败'
      return
    }
    await load()
    digest.value = got
    digestNote.value = '已写入作品，发布页直接用'
  } finally {
    digestBusy.value = false
  }
}

function goto(item) {
  emit('goto', item)
  close()
}

function onKey(e) {
  if (e.key === 'Escape' && props.open) close()
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
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
  width: min(960px, 100%);
  max-height: 90vh;
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
.head-main {
  flex: 1;
  min-width: 0;
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
  font-family: theme('fontFamily.mono');
  font-size: 11.5px;
  color: var(--text-faint);
}
.x {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.x:hover {
  background: rgba(255, 255, 255, 0.07);
  color: var(--text);
}
.dlg-body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 16px;
  padding: 16px 18px;
  overflow: hidden;
}
.phone {
  flex-shrink: 0;
  width: 399px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
  background: #fff;
}
.phone-bar {
  padding: 6px 10px;
  border-bottom: 1px solid var(--line-soft);
  background: var(--panel-2);
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.phone-screen {
  flex: 1;
  min-height: 0;
  padding: 18px 16px 24px;
  overflow-y: auto;
  background: #fff;
}
.side {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.blk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.blk h3 {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-muted);
}
.badge {
  padding: 0 5px;
  border-radius: 8px;
  background: rgba(214, 108, 96, 0.18);
  font-size: 10px;
  line-height: 15px;
  color: #d9705f;
}
.txt {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text);
  word-break: break-word;
}
.warn {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--warn);
}
.hint {
  margin: 0;
  font-size: 11px;
  color: var(--text-faint);
}
.blk .btn {
  align-self: flex-start;
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
}
.checks {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.checks li {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--line-soft);
  border-left: 2px solid var(--line);
  border-radius: 5px;
  background: var(--panel-2);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
.checks li.warn {
  border-left-color: rgba(214, 108, 96, 0.55);
  color: var(--text);
}
.msg {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}
.mini {
  flex-shrink: 0;
  padding: 0 6px;
  height: 20px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: transparent;
  font-size: 10.5px;
  color: var(--text-faint);
  cursor: pointer;
}
.mini:hover {
  color: var(--run);
  border-color: var(--run);
}
.imgs {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
.imgs .sec {
  margin-right: 6px;
  color: var(--text-faint);
}
.dlg-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 18px 14px;
  border-top: 1px solid var(--line-soft);
}
.tip {
  flex: 1;
  font-size: 11px;
  color: var(--text-faint);
}
.strong {
  font-size: 13px;
  font-weight: 600;
}
.picks {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.picks li {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 5px 7px;
  border: 1px solid var(--line-soft);
  border-radius: 5px;
  background: var(--panel-2);
  font-size: 11.5px;
  line-height: 1.6;
}
.pick-text {
  flex: 1;
  min-width: 0;
  color: var(--text);
  word-break: break-word;
}
.pick-n {
  flex-shrink: 0;
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  color: var(--text-faint);
}
.c-angle {
  margin-right: 5px;
  padding: 0 4px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.08);
  font-size: 10px;
  color: var(--text-faint);
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: transparent;
  font-size: 11px;
  color: var(--text-muted);
  cursor: pointer;
}
.chip:hover {
  color: var(--text);
  border-color: var(--text-faint);
}
.chip.on {
  border-color: var(--run);
  color: var(--run);
}
.field {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--panel-2);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text);
  resize: vertical;
}
.field:focus {
  outline: none;
  border-color: var(--run);
}
.row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-faint);
  cursor: pointer;
}
</style>
