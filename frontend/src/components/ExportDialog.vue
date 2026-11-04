<template>
  <div v-if="open" class="mask" @click.self="close">
    <div class="dlg" role="dialog" aria-modal="true" aria-label="导出成稿">
      <header class="dlg-head">
        <div class="head-main">
          <h2>导出成稿</h2>
          <p class="dlg-sub">{{ workTitle }} · {{ stats }}</p>
        </div>
        <button class="x" aria-label="关闭" @click="close">
          <AppIcon name="x" :size="15" />
        </button>
      </header>

      <div class="dlg-body">
        <section class="blk">
          <h3>格式</h3>
          <div class="formats" role="radiogroup" aria-label="导出格式">
            <button
              v-for="f in FORMATS"
              :key="f.id"
              type="button"
              class="fmt"
              :class="{ 'is-on': format === f.id }"
              role="radio"
              :aria-checked="format === f.id"
              @click="format = f.id"
            >
              <span class="fmt-name">{{ f.name }}</span>
              <span class="fmt-ext">.{{ f.id }}</span>
              <span class="fmt-desc">{{ f.desc }}</span>
            </button>
          </div>
        </section>

        <section class="blk">
          <h3>选项</h3>
          <label class="check">
            <input v-model="includeEmpty" type="checkbox" />
            <span>连还没有正文的节一起导出（留占位标题）</span>
          </label>
          <label v-if="format === 'docx' || format === 'epub'" class="field">
            <span class="field-label">作者署名</span>
            <input v-model.trim="author" class="input" placeholder="可留空" maxlength="40" />
          </label>
        </section>

        <p class="hint">
          TXT / DOCX / EPUB 都按全书阅读顺序拼装，分卷自动成为一级标题。
          公众号版请走顶部「公众号」。
        </p>
        <p v-if="note" class="err">{{ note }}</p>
      </div>

      <footer class="dlg-foot">
        <button class="btn" @click="close">取消</button>
        <button class="btn btn-primary" :disabled="busy" @click="doExport">
          <AppIcon name="download" :size="14" />
          {{ busy ? '正在导出…' : '下载' }}
        </button>
      </footer>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['update:open'])
const store = useStudioStore()

const FORMATS = [
  { id: 'md', name: 'Markdown', desc: '存档、进 Git、再加工' },
  { id: 'txt', name: '纯文本', desc: '任何编辑器都能打开' },
  { id: 'docx', name: 'Word', desc: '带标题层级，交给编辑' },
  { id: 'epub', name: 'EPUB', desc: '手机阅读器直接翻' },
]

const format = ref('docx')
const includeEmpty = ref(false)
const author = ref('')
const busy = ref(false)
const note = ref('')

const workTitle = computed(() => store.work?.title || '未命名作品')
const stats = computed(() => {
  const n = store.tree.filter((x) => x.ordinal != null).length
  const words = store.stats?.words || 0
  return `${n} 节 · ${words.toLocaleString()} 字`
})

watch(() => props.open, (open) => {
  if (!open) return
  note.value = ''
  try {
    author.value = localStorage.getItem('studio:export-author') || ''
  } catch {
    author.value = ''
  }
})

function close() {
  emit('update:open', false)
}

async function doExport() {
  if (busy.value) return
  busy.value = true
  note.value = ''
  try {
    const ok = await store.downloadExport({
      format: format.value,
      includeEmpty: includeEmpty.value,
      author: author.value,
    })
    if (!ok) {
      note.value = store.error || '导出失败'
      return
    }
    try {
      localStorage.setItem('studio:export-author', author.value)
    } catch {
      /* 存不下署名不影响导出 */
    }
    close()
  } finally {
    busy.value = false
  }
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
  width: min(560px, 100%);
  max-height: 90vh;
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
  background: rgba(var(--over), 0.07);
  color: var(--text);
}
.dlg-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 18px;
}
.blk + .blk {
  margin-top: 18px;
}
.blk h3 {
  margin: 0 0 9px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-faint);
}
.formats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.fmt {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
  padding: 10px 12px;
  border: 1px solid var(--line-soft);
  border-radius: 9px;
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.15s var(--ease), background-color 0.15s var(--ease);
}
.fmt:hover {
  border-color: var(--line);
  background: rgba(var(--over), 0.03);
}
.fmt.is-on {
  border-color: var(--run);
  background: rgba(var(--run-rgb), 0.08);
}
.fmt-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.fmt-ext {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.fmt-desc {
  flex-basis: 100%;
  font-size: 11.5px;
  color: var(--text-faint);
}
.check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-muted);
  cursor: pointer;
}
.field {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}
.field-label {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-faint);
}
.input {
  flex: 1;
  min-width: 0;
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--line-soft);
  border-radius: 7px;
  background: var(--panel);
  color: var(--text);
  font-size: 12.5px;
}
.input:focus {
  outline: none;
  border-color: var(--run);
}
.hint {
  margin: 16px 0 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-faint);
}
.err {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--warn);
}
.dlg-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px 16px;
  border-top: 1px solid var(--line-soft);
}
</style>
