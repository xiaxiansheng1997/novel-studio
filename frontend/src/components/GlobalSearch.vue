<template>
  <div v-if="open" class="mask" @click.self="close">
    <div class="box" role="dialog" aria-modal="true" aria-label="全库搜索">
      <header class="head">
        <AppIcon name="search" :size="15" />
        <input
          ref="inputEl"
          v-model="q"
          class="q"
          placeholder="搜标题、主旨、正文…"
          aria-label="全库搜索关键词"
          @keydown="onKey"
        />
        <button class="esc" title="关闭（Esc）" @click="close">
          <span>Esc</span>
          <AppIcon name="x" :size="12" />
        </button>
      </header>

      <div class="body">
        <p v-if="!q.trim()" class="tip">
          搜的是作品标题、主旨，以及已建索引的正文与素材。
        </p>
        <p v-else-if="busy" class="tip">搜索中…</p>
        <template v-else>
          <section v-if="works.length" class="group">
            <p class="group-title">作品</p>
            <button
              v-for="(w, i) in works"
              :key="w.id"
              class="hit"
              :class="{ 'is-cursor': cursor === i }"
              @click="pick({ work_id: w.id })"
            >
              <span class="hit-title">{{ w.title }}</span>
              <span class="hit-sub">
                {{ w.status === 'archived' ? '已归档' : '在写' }}
                <template v-if="w.premise"> · {{ w.premise }}</template>
              </span>
            </button>
          </section>

          <section v-if="hits.length" class="group">
            <p class="group-title">正文 · {{ hits.length }} 处</p>
            <button
              v-for="(h, i) in hits"
              :key="h.work_id + h.node_id + i"
              class="hit"
              :class="{ 'is-cursor': cursor === works.length + i }"
              @click="pick(h)"
            >
              <span class="hit-title">
                {{ h.work_title }}<template v-if="h.node_title"> · {{ h.node_title }}</template>
                <span v-if="h.source_kind === 'source'" class="tag">素材</span>
              </span>
              <span class="hit-text">{{ h.snippet }}</span>
            </button>
          </section>

          <p v-if="!works.length && !hits.length" class="tip">
            没找到「{{ q }}」。只搜得到已建索引的正文——旧作品先进去跑一次「索引」。
          </p>
        </template>
      </div>

      <footer class="foot">
        <span>↑↓ 选择 · Enter 打开 · Esc 关闭</span>
        <span class="foot-tip">Ctrl+K 随时唤起</span>
      </footer>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  open: { type: Boolean, default: false },
  // 书架空态点「去正文里搜」时带进来的词：打开即有结果，不用重打一遍
  initial: { type: String, default: '' },
})
const emit = defineEmits(['update:open', 'goto'])
const store = useStudioStore()

const q = ref('')
const inputEl = ref(null)
const busy = ref(false)
const works = ref([])
const hits = ref([])
const cursor = ref(0)
let timer = 0
// 请求序号：慢的那次后回来时不能把新结果盖掉
let seq = 0

const items = computed(() => [
  ...works.value.map((w) => ({ work_id: w.id })),
  ...hits.value.map((h) => ({ work_id: h.work_id, node_id: h.node_id })),
])

watch(() => props.open, async (on) => {
  clearTimeout(timer)
  if (!on) return
  const seed = (props.initial || '').trim()
  if (seed === q.value) {
    // 带着同一句再打开：结果可能已经被清空，直接重搜一次
    if (seed) await search(seed)
  } else {
    q.value = seed
    works.value = []
    hits.value = []
    cursor.value = 0
    busy.value = false
  }
  await nextTick()
  inputEl.value?.focus()
})

watch(q, () => {
  clearTimeout(timer)
  const text = q.value.trim()
  cursor.value = 0
  if (!text) {
    works.value = []
    hits.value = []
    busy.value = false
    return
  }
  // 每敲一个字就发一次请求太吵：停手 280ms 再搜。
  // busy 立刻置上，否则这 280ms 里会闪一下「没找到」。
  busy.value = true
  timer = setTimeout(() => search(text), 280)
})

async function search(text) {
  const mine = ++seq
  busy.value = true
  const data = await store.globalSearch(text)
  if (mine !== seq) return
  works.value = data.works || []
  hits.value = data.hits || []
  busy.value = false
}

function close() {
  emit('update:open', false)
}

function pick(hit) {
  if (!hit?.work_id) return
  emit('goto', hit)
  close()
}

function move(delta) {
  const n = items.value.length
  if (!n) return
  cursor.value = (cursor.value + delta + n) % n
}

function onKey(e) {
  if (e.key === 'Escape') {
    e.preventDefault()
    close()
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    move(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    move(-1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    pick(items.value[cursor.value])
  }
}
</script>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 130;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 12vh 24px 24px;
  background: var(--overlay);
}
.box {
  width: min(640px, 100%);
  max-height: 70vh;
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
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-faint);
}
.q {
  flex: 1;
  min-width: 0;
  height: 28px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 14px;
  outline: none;
}
.q::placeholder {
  color: var(--text-faint);
}
.esc {
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
  padding: 10px;
}
.tip {
  margin: 6px 6px 10px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-faint);
}
.group {
  margin-bottom: 8px;
}
.group-title {
  margin: 4px 6px 6px;
  font-size: 10.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.hit {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 8px 9px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: background-color 0.15s var(--ease), border-color 0.15s var(--ease);
}
.hit:hover,
.hit.is-cursor {
  background: rgba(var(--over), 0.045);
  border-color: var(--line-soft);
}
.hit-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text);
}
.hit-sub {
  font-size: 11px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hit-text {
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tag {
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(var(--over), 0.06);
  color: var(--text-faint);
  font-size: 10px;
}
.foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  border-top: 1px solid var(--line-soft);
  font-size: 10.5px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.foot-tip {
  opacity: 0.7;
}
</style>
