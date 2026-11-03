<template>
  <div class="read" :style="writerVars">
    <header class="bar">
      <button class="bar-btn" title="返回写作台（E）" @click="back">‹ 回写作台</button>
      <div class="bar-title">
        <strong>{{ data?.title || '阅读' }}</strong>
        <span class="bar-meta">{{ loading ? '载入中…' : `${sections.length} 节 · ${chars.toLocaleString()} 字` }}</span>
      </div>
      <div class="bar-acts">
        <button class="bar-btn" title="小一号（Ctrl+-）" @click="bumpFontDown">A−</button>
        <button class="bar-btn" title="大一号（Ctrl+=）" @click="bumpFontUp">A＋</button>
        <button
          class="bar-btn"
          :class="{ 'is-on': writer.tint }"
          title="护眼底色"
          @click="setTint(!writer.tint)"
        >护眼</button>
        <button
          class="bar-btn"
          :class="{ 'is-on': tocOpen }"
          title="目录（Ctrl+T）"
          @click="tocOpen = !tocOpen"
        >目录</button>
      </div>
    </header>

    <div class="stage">
      <nav v-if="tocOpen" class="toc" aria-label="全书目录">
        <template v-for="sec in sections" :key="`t-${sec.id}`">
          <p v-if="sec.parent_title && sec.parent_title !== lastShown(sec)" class="toc-vol">
            {{ sec.parent_title }}
          </p>
          <a
            class="toc-item"
            :class="{ 'is-here': activeId === sec.id }"
            :href="`#rd-${sec.id}`"
            @click.prevent="jump(sec.id)"
          >{{ sec.title }}</a>
        </template>
      </nav>

      <main ref="docEl" class="doc">
        <p v-if="loading" class="empty">正在把全书按阅读顺序拼起来…</p>
        <p v-else-if="!sections.length" class="empty">
          还没有能读的内容：先写几节，或点「逐节生成」。
        </p>

        <template v-for="sec in sections" :key="sec.id">
          <h2
            v-if="sec.parent_title && sec.parent_title !== lastShown(sec)"
            class="vol-head"
          >{{ sec.parent_title }}</h2>
          <section :id="`rd-${sec.id}`" class="sec">
            <h3 class="sec-head">{{ sec.title }}</h3>
            <!-- 文章按 Markdown 渲染；小说认段落，正文里不该有井号 -->
            <div v-if="isArticle" class="md" v-html="renderMd(sec.content)"></div>
            <template v-else>
              <p v-for="(para, i) in paras(sec.content)" :key="i">{{ para }}</p>
            </template>
          </section>
        </template>

        <p v-if="!loading && sections.length" class="the-end">— 完 —</p>
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { marked } from 'marked'
import { useStudioStore } from '../stores/studio'
import {
  bumpFontDown, bumpFontUp, setTint, writer, writerVars,
} from '../utils/writer'

const route = useRoute()
const router = useRouter()
const store = useStudioStore()

const data = ref(null)
const loading = ref(true)
const tocOpen = ref(true)
const docEl = ref(null)
const activeId = ref('')

const sections = computed(() => data.value?.sections || [])
const chars = computed(() => data.value?.chars || 0)
// 只有「文章」走 Markdown：小说正文里的井号是要读的字，不是标记
const isArticle = computed(() => (data.value?.kind || 'article') === 'article')

const byId = computed(() => {
  const m = {}
  for (const s of sections.value) m[s.id] = s
  return m
})

/** 上一节属于哪个容器：卷标题只在换卷时出现一次（与导出渲染同一口径） */
function lastShown(sec) {
  const idx = sections.value.indexOf(sec)
  return idx > 0 ? sections.value[idx - 1].parent_title : ''
}

function paras(text) {
  return (text || '').split(/\n{2,}/).map((p) => p.trim()).filter(Boolean)
}

function renderMd(text) {
  if (!text) return ''
  return marked(text, { breaks: true, gfm: true })
}

function jump(id) {
  const el = document.getElementById(`rd-${id}`)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  activeId.value = id
}

function back() {
  router.push(`/studio/${route.params.id}`)
}

// 当前读到哪节：目录跟着高亮
let observer = null
function watchSections() {
  if (observer) observer.disconnect()
  observer = new IntersectionObserver(
    (entries) => {
      for (const en of entries) {
        if (en.isIntersecting) {
          activeId.value = en.target.id.replace(/^rd-/, '')
          break
        }
      }
    },
    { rootMargin: '-15% 0px -70% 0px', threshold: 0 },
  )
  for (const s of sections.value) {
    const el = document.getElementById(`rd-${s.id}`)
    if (el) observer.observe(el)
  }
}

async function load() {
  const id = String(route.params.id || '')
  loading.value = true
  try {
    if (!id) return
    // 直接从 URL 进阅读页时 store 还没选中作品：先补上下文再拉数据
    if (store.work?.id !== id) await store.selectWork(id)
    data.value = await store.fetchReading(id)
    await nextTick()
    watchSections()
  } finally {
    loading.value = false
  }
}

function onKeydown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 't') {
    e.preventDefault()
    tocOpen.value = !tocOpen.value
  } else if ((e.ctrlKey || e.metaKey) && e.code === 'Equal' && !e.altKey && !e.shiftKey) {
    e.preventDefault()
    bumpFontUp()
  } else if ((e.ctrlKey || e.metaKey) && e.code === 'Minus' && !e.altKey) {
    e.preventDefault()
    bumpFontDown()
  } else if (e.key === 'Escape' && tocOpen.value) {
    tocOpen.value = false
  } else if (e.key.toLowerCase() === 'e' && !(e.ctrlKey || e.metaKey || e.altKey)
    && !['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target?.tagName)) {
    back()
  }
}

onMounted(() => {
  load()
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  if (observer) observer.disconnect()
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.read {
  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  height: 100%;
  background: var(--bg);
}

/* ---------- 顶栏 ---------- */
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--line);
  background: var(--panel);
  flex: none;
}
.bar-title {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.bar-title strong {
  font-family: theme('fontFamily.serif');
  font-size: 14.5px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bar-meta {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.bar-acts {
  display: flex;
  gap: 4px;
}
.bar-btn {
  padding: 4px 10px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: transparent;
  color: var(--text-dim, var(--text));
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}
.bar-btn:hover {
  border-color: rgba(var(--run-rgb), 0.45);
  color: var(--text);
}
.bar-btn.is-on {
  border-color: rgba(var(--run-rgb), 0.55);
  background: rgba(var(--run-rgb), 0.1);
  color: var(--run-fg);
}

/* ---------- 舞台：目录 + 正文 ---------- */
.stage {
  flex: 1;
  min-height: 0;
  display: flex;
}
.toc {
  width: 216px;
  flex: none;
  overflow-y: auto;
  padding: 14px 10px 40px;
  border-right: 1px solid var(--line-soft);
  background: var(--panel);
}
.toc-vol {
  margin: 12px 6px 4px;
  font-size: 10.5px;
  letter-spacing: 0.08em;
  color: var(--text-faint);
}
.toc-item {
  display: block;
  padding: 4px 8px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-dim, var(--text));
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.toc-item:hover {
  background: rgba(var(--over), 0.06);
}
.toc-item.is-here {
  color: var(--run-fg);
  background: rgba(var(--run-rgb), 0.1);
}

.doc {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 34px 24px 90px;
  background: var(--w-tint-bg);
}
/* 正文排版全部吃 utils/writer.js 的那组变量：与写作台一处调、两处跟着变 */
.doc p {
  margin: 0 0 1em;
  font-family: var(--w-font);
  font-size: var(--w-size);
  line-height: var(--w-height);
  color: var(--text);
  max-width: var(--w-measure);
}
.vol-head {
  margin: 34px 0 8px;
  padding-top: 20px;
  border-top: 1px solid var(--line);
  font-family: theme('fontFamily.serif');
  font-size: calc(var(--w-size) + 6px);
  font-weight: 700;
  color: var(--text);
  max-width: var(--w-measure);
}
.sec {
  margin-bottom: 26px;
  scroll-margin-top: 16px;
}
.sec-head {
  margin: 0 0 14px;
  font-family: theme('fontFamily.serif');
  font-size: calc(var(--w-size) + 2px);
  font-weight: 600;
  color: var(--text);
  max-width: var(--w-measure);
}
.empty,
.the-end {
  margin: 30px auto;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-faint);
}
.the-end {
  font-family: theme('fontFamily.serif');
  font-size: 14px;
}

/* 小说段落用首行缩进更像成书；段间不空行也读得开 */
.doc:not(.as-md) p + p {
  text-indent: 2em;
}

/* Markdown 渲染（文章）：只给基本元素配版式，细节跟写作台预览一致 */
.md :deep(p) {
  text-indent: 0;
}
.md :deep(h1),
.md :deep(h2),
.md :deep(h3),
.md :deep(h4) {
  margin: 1.2em 0 0.6em;
  font-family: theme('fontFamily.serif');
  color: var(--text);
  max-width: var(--w-measure);
}
.md :deep(h1) { font-size: calc(var(--w-size) + 6px); }
.md :deep(h2) { font-size: calc(var(--w-size) + 3px); }
.md :deep(h3) { font-size: calc(var(--w-size) + 1px); }
.md :deep(ul),
.md :deep(ol) {
  margin: 0 0 1em;
  padding-left: 1.6em;
  font-family: var(--w-font);
  font-size: var(--w-size);
  line-height: var(--w-height);
  color: var(--text);
  max-width: var(--w-measure);
}
.md :deep(li) {
  margin: 0.2em 0;
}
.md :deep(blockquote) {
  margin: 0 0 1em;
  padding: 2px 14px;
  border-left: 3px solid var(--line);
  color: var(--text-dim, var(--text));
  max-width: var(--w-measure);
}
.md :deep(pre) {
  overflow-x: auto;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(var(--over), 0.05);
  font-family: theme('fontFamily.mono');
  font-size: calc(var(--w-size) - 2px);
  line-height: 1.6;
}
.md :deep(code) {
  font-family: theme('fontFamily.mono');
  font-size: calc(var(--w-size) - 2px);
}
.md :deep(hr) {
  margin: 1.6em 0;
  border: none;
  border-top: 1px solid var(--line);
}
.md :deep(img) {
  max-width: 100%;
}
.md :deep(a) {
  color: var(--run-fg);
  text-decoration: underline;
  text-underline-offset: 2px;
}
</style>
