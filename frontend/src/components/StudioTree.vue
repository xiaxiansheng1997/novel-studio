<template>
  <aside class="tree" aria-label="作品结构">
    <header class="tree-head">
      <span class="tree-title">{{ title }}</span>
      <button class="tree-all" :title="allFolded ? '全部展开' : '全部收起'" @click="toggleAll">
        <AppIcon :name="allFolded ? 'chevronDown' : 'chevronUp'" :size="13" />
      </button>
      <button class="icon-btn tree-add" title="把已有稿件导入成节" @click="$emit('import')">
        <AppIcon name="download" :size="14" />
      </button>
      <button class="icon-btn tree-add" title="新建节点" @click="$emit('create', null)">
        <AppIcon name="plus" :size="15" />
      </button>
    </header>

    <div class="tree-body">
      <p v-if="!tree.length" class="tree-empty">
        还没有内容。先建一个「卷」或「篇」，再在里面建章/节。
      </p>

      <div
        v-for="n in visibleRows"
        :key="n.id"
        class="tree-row"
        :class="{ 'is-active': n.id === activeId }"
        :style="{ paddingLeft: 6 + Math.min(n.depth, 3) * 14 + 'px' }"
        @click="$emit('select', n.id)"
      >
        <!-- 卷下面几十章时，把写完的卷收起来比拖滚动条好用 -->
        <button
          v-if="childful.has(n.id)"
          class="tree-twist"
          :class="{ 'is-folded': collapsed.has(n.id) }"
          :title="collapsed.has(n.id) ? '展开' : '收起'"
          :aria-expanded="!collapsed.has(n.id)"
          @click.stop="toggle(n.id)"
        >
          <AppIcon name="chevronDown" :size="12" />
        </button>
        <span v-else class="tree-twist is-ghost" aria-hidden="true"></span>
        <AppIcon
          :name="n.depth === 0 ? 'folder' : 'file'"
          :size="14"
          class="tree-icon"
        />
        <span class="tree-label" :title="n.title">{{ n.title }}</span>
        <span v-if="n.ordinal != null && n.word_count" class="tree-words">{{ fmt(n.word_count) }}</span>
        <span class="tree-dot" :class="n.status"></span>
        <span class="tree-ops">
          <button
            v-if="n.ordinal != null"
            class="op"
            title="上移"
            :disabled="rankOf(n.id).first"
            @click.stop="$emit('move', n, 'up')"
          >
            <AppIcon name="chevronUp" :size="12" />
          </button>
          <button
            v-if="n.ordinal != null"
            class="op"
            title="下移"
            :disabled="rankOf(n.id).last"
            @click.stop="$emit('move', n, 'down')"
          >
            <AppIcon name="chevronDown" :size="12" />
          </button>
          <button class="op" title="在此节点下新建" @click.stop="$emit('create', n)">
            <AppIcon name="plus" :size="12" />
          </button>
          <button
            class="op"
            title="删除（连同子树与全部历史版本；正文会先备份，可用 scripts/restore_backup.py 找回）"
            @click.stop="$emit('remove', n)"
          >
            <AppIcon name="trash" :size="12" />
          </button>
        </span>
      </div>
    </div>

    <footer v-if="stats" class="tree-foot">
      <span>{{ stats.nodes }} 个节点</span>
      <span class="tree-sep">·</span>
      <span>{{ fmt(stats.words) }}<template v-if="target"> / {{ fmt(target) }}</template> 字</span>
    </footer>
  </aside>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  tree: { type: Array, default: () => [] },
  activeId: { type: String, default: '' },
  title: { type: String, default: '结构' },
  stats: { type: Object, default: null },
  // 作品目标字数（0 = 没设）：树底显示「已写 / 目标」，比只报数字有方向
  target: { type: Number, default: 0 },
  // 折叠状态按作品分开存：同一本书下次打开还是上次收起来的样子
  workId: { type: String, default: '' },
})
defineEmits(['select', 'create', 'remove', 'move', 'import'])

// 树是扁平数组（按 path 排序），折叠只影响这一屏的渲染，不动数据结构
const collapsed = ref(new Set())
const storageKey = computed(() => `studio:tree-folded:${props.workId || 'default'}`)

function readFolded() {
  try {
    const raw = localStorage.getItem(storageKey.value)
    const list = raw ? JSON.parse(raw) : []
    collapsed.value = new Set(Array.isArray(list) ? list : [])
  } catch {
    // 隐私模式或坏数据：折叠失效而已，树得照常显示，不能因此白屏
    collapsed.value = new Set()
  }
}

function persist() {
  try {
    localStorage.setItem(storageKey.value, JSON.stringify([...collapsed.value]))
  } catch {
    /* 写不进去就只在本次会话有效 */
  }
}

readFolded()

// 有子节点的行才给三角：扁平数组里「下一行更深」就是有孩子
const childful = computed(() => {
  const out = new Set()
  const t = props.tree
  for (let i = 0; i < t.length - 1; i += 1) {
    if (t[i + 1].depth > t[i].depth) out.add(t[i].id)
  }
  return out
})

const allFolded = computed(
  () => childful.value.size > 0 && [...childful.value].every((id) => collapsed.value.has(id))
)

/**
 * 可见行：沿扁平数组走一遍，用栈记住祖先；栈里任何一层被收起，这一行就不出现。
 * 被藏住的行也要入栈——否则它的子孙会被当成根节点漏出来。
 */
const visibleRows = computed(() => {
  const out = []
  const stack = []
  for (const n of props.tree) {
    while (stack.length && stack[stack.length - 1].depth >= n.depth) stack.pop()
    if (!stack.some((a) => collapsed.value.has(a.id))) out.push(n)
    stack.push(n)
  }
  return out
})

function toggle(id) {
  const next = new Set(collapsed.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  collapsed.value = next
  persist()
}

function toggleAll() {
  collapsed.value = allFolded.value ? new Set() : new Set(childful.value)
  persist()
}

function ancestorsOf(index) {
  const out = []
  let depth = props.tree[index].depth
  for (let i = index - 1; i >= 0 && depth > 0; i -= 1) {
    if (props.tree[i].depth < depth) {
      out.push(props.tree[i].id)
      depth = props.tree[i].depth
    }
  }
  return out
}

/** 选中被折叠的节时先把祖先展开：从搜索结果跳进来，不该看到「选中了但人不在」。 */
function revealActive() {
  if (!props.activeId) return
  const idx = props.tree.findIndex((n) => n.id === props.activeId)
  if (idx < 0) return
  const hidden = ancestorsOf(idx).filter((id) => collapsed.value.has(id))
  if (!hidden.length) return
  const next = new Set(collapsed.value)
  hidden.forEach((id) => next.delete(id))
  collapsed.value = next
  persist()
}

watch(() => [props.activeId, props.tree.length], revealActive, { immediate: true })
watch(() => props.workId, () => {
  readFolded()
  revealActive()
})

// 同层上下移只能跟同一个父节点下的兄弟换：后端也是这么找邻居的（只在同 parent_id 里取
// 紧邻的前/后一个），所以「首/末行该不该置灰」也按同一套分组算。
// 树是按 path 返回的，同一父节点的子节点会被孙节点隔开，因此不能按相邻行分组，
// 得先按 parent_id 归堆。只统计参与排序的那些（容器节点 ordinal 为空，它不移动）。
const ranks = computed(() => {
  const groups = new Map()
  for (const n of props.tree) {
    if (n.ordinal == null) continue
    const key = n.parent_id || ''
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(n.id)
  }
  const out = new Map()
  for (const ids of groups.values()) {
    ids.forEach((id, i) => out.set(id, { first: i === 0, last: i === ids.length - 1 }))
  }
  return out
})

function rankOf(id) {
  return ranks.value.get(id) || { first: true, last: true }
}

function fmt(n) {
  if (!n) return '0'
  if (n >= 10000) return (n / 10000).toFixed(1) + '万'
  return String(n)
}
</script>

<style scoped>
.tree {
  width: 252px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border-right: 1px solid var(--line-soft);
  min-height: 0;
}
.tree-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 10px 10px 14px;
  border-bottom: 1px solid var(--line-soft);
}
.tree-title {
  font-size: 11.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-faint);
}
.tree-all {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  transition: background-color 0.15s var(--ease), color 0.15s var(--ease);
}
.tree-all:hover {
  background: rgba(var(--over), 0.08);
  color: var(--text);
}
.tree-add {
  margin-left: 2px;
  width: 26px;
  height: 26px;
}
.tree-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px 6px;
}
.tree-empty {
  margin: 16px 10px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.tree-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
  height: 32px;
  padding-right: 8px;
  border-radius: 7px;
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
  transition: background-color 0.15s var(--ease), color 0.15s var(--ease);
}
.tree-row:hover {
  background: rgba(var(--over), 0.035);
  color: var(--text);
}
.tree-row.is-active {
  background: rgba(var(--run-rgb), 0.1);
  color: var(--text);
}
.tree-row.is-active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 7px;
  bottom: 7px;
  width: 2px;
  border-radius: 2px;
  background: var(--run);
}
.tree-twist {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  margin-left: -2px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  transition: transform 0.15s var(--ease), background-color 0.15s var(--ease);
}
.tree-twist.is-folded {
  transform: rotate(-90deg);
}
/* 叶子节点留同宽的占位：有没有三角，标题的起始位置都一致 */
.tree-twist.is-ghost {
  cursor: default;
}
.tree-twist.is-ghost:hover {
  background: transparent;
  color: var(--text-faint);
}
.tree-icon {
  flex-shrink: 0;
  color: var(--text-faint);
}
.tree-row.is-active .tree-icon {
  color: var(--run);
}
.tree-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tree-words {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
}
.tree-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--text-faint);
  opacity: 0.5;
}
.tree-dot.final {
  background: var(--run);
  opacity: 1;
}
.tree-dot.review {
  background: var(--warn);
  opacity: 1;
}
.tree-ops {
  display: none;
  align-items: center;
  gap: 2px;
}
.tree-row:hover .tree-ops {
  display: flex;
}
.tree-row:hover .tree-words,
.tree-row:hover .tree-dot {
  display: none;
}
.op {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.op:hover {
  background: rgba(var(--over), 0.08);
  color: var(--text);
}
/* 到边了不是出错：置灰但保留占位，不然一行宽度会随悬停跳动 */
.op:disabled {
  opacity: 0.28;
  cursor: default;
}
.op:disabled:hover {
  background: transparent;
  color: var(--text-faint);
}
.tree-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 9px 14px;
  border-top: 1px solid var(--line-soft);
  font-size: 11px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.tree-sep {
  opacity: 0.5;
}
</style>
