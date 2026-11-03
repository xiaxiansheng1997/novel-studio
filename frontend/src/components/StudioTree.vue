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
        :class="{
          'is-active': n.id === activeId,
          'is-picked': selectedIds.includes(n.id),
          'is-dragging': dragNode && dragNode.id === n.id,
          'is-drop-inside': dropPos.id === n.id && dropPos.pos === 'inside',
          'is-drop-before': dropPos.id === n.id && dropPos.pos === 'before',
          'is-drop-after': dropPos.id === n.id && dropPos.pos === 'after',
        }"
        :style="{ paddingLeft: 6 + Math.min(n.depth, 3) * 14 + 'px' }"
        :draggable="n.ordinal != null"
        @click="$emit('select', n.id)"
        @click.shift.prevent="$emit('update:selected', rangeTo(n.id))"
        @click.ctrl.prevent="$emit('update:selected', togglePick(n.id))"
        @click.meta.prevent="$emit('update:selected', togglePick(n.id))"
        @contextmenu.prevent.stop="openMenu($event, n)"
        @dragstart="onDragStart($event, n)"
        @dragend="onDragEnd"
        @dragover="onDragOver($event, n)"
        @dragleave="onDragLeave($event, n)"
        @drop="onDrop($event, n)"
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
            class="op"
            title="上移"
            :disabled="rankOf(n.id).first"
            @click.stop="$emit('move', n, 'up')"
          >
            <AppIcon name="chevronUp" :size="12" />
          </button>
          <button
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

    <footer v-if="selectedIds.length" class="tree-batch">
      <span class="batch-n">已选 {{ selectedIds.length }} 节</span>
      <select v-model="batchTarget" class="batch-sel" title="把选中的节整体搬进这个容器">
        <option value="">搬入…</option>
        <option v-for="c in containers" :key="c.id" :value="c.id">{{ '　'.repeat(c.depth) }}{{ c.title }}</option>
      </select>
      <button class="op" title="把选中的节移入上面选的容器（按原顺序）" :disabled="!batchTarget" @click="emitBatch('move')">
        <AppIcon name="folder" :size="12" />
      </button>
      <button class="op" title="把连续相邻的选中节逐对合并（正文拼进最前一节，其余节先备份再删）" @click="emitBatch('merge')">⤵</button>
      <button class="op" title="删除选中的节（每节删前落备份）" @click="emitBatch('remove')">
        <AppIcon name="trash" :size="12" />
      </button>
      <button class="op" title="取消选择" @click="$emit('update:selected', [])">
        <AppIcon name="x" :size="12" />
      </button>
    </footer>

    <footer v-else-if="stats" class="tree-foot">
      <span>{{ stats.nodes }} 个节点</span>
      <span class="tree-sep">·</span>
      <span>{{ fmt(stats.words) }}<template v-if="target"> / {{ fmt(target) }}</template> 字</span>
    </footer>

    <!-- 右键菜单：挂在 body 上，不受树栏 overflow 裁剪 -->
    <Teleport to="body">
      <div v-if="menu.node" class="ctx-mask" @click="closeMenu" @contextmenu.prevent="closeMenu">
        <div class="ctx-menu" :style="{ left: menu.x + 'px', top: menu.y + 'px' }" role="menu">
          <p class="ctx-title">{{ menu.node.title }}</p>
          <button
            v-for="it in menuItems"
            :key="it.act"
            class="ctx-item"
            :class="{ 'is-danger': it.danger, 'is-off': it.disabled }"
            role="menuitem"
            :disabled="it.disabled"
            @click="runMenu(it)"
          >
            <span class="ctx-label">{{ it.label }}</span>
            <span v-if="it.hint" class="ctx-hint">{{ it.hint }}</span>
          </button>
        </div>
      </div>
    </Teleport>
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
  // 多选（Ctrl/Shift 点选）：集合存在父层，树只负责圈出来与发起批量动作
  selectedIds: { type: Array, default: () => [] },
  // 当前正文非空、能在光标处拆分的节 id（拆分入口只对这一节亮着）
  splittableId: { type: String, default: '' },
})
const emit = defineEmits(['select', 'create', 'remove', 'move', 'import', 'reparent',
  'update:selected', 'duplicate', 'split', 'merge', 'insert', 'batch'])

// 拖动的是哪一行（只允许有 ordinal 的叶子），以及当前悬停的落点
const dragNode = ref(null)
const dropPos = ref({ id: '', pos: '' })

function onDragStart(e, n) {
  if (n.ordinal == null) return
  dragNode.value = n
  e.dataTransfer.effectAllowed = 'move'
  // 有的浏览器不写 data 就不派发 drop，写一个无害的
  e.dataTransfer.setData('text/plain', n.id)
}

function onDragEnd() {
  dragNode.value = null
  dropPos.value = { id: '', pos: '' }
}

/**
 * 落点分三种：行上沿=插到它前面、行下沿=插到它后面、行中间=放进它里面。
 * 只有容器（卷/篇）或已经有子节点的章接得住「里面」；普通叶子只在上下沿接。
 * 拖进自己的子树直接不接，免得后端再报一次「不能拖进自己的子树」。
 */
function onDragOver(e, n) {
  const src = dragNode.value
  if (!src || n.id === src.id) return
  if (n.path && src.path && n.path.startsWith(`${src.path}.`)) return
  const rect = e.currentTarget.getBoundingClientRect()
  const ratio = rect.height ? (e.clientY - rect.top) / rect.height : 0.5
  let pos
  if (n.ordinal == null) {
    pos = 'inside'
  } else if (ratio < 0.28) {
    pos = 'before'
  } else if (ratio > 0.72) {
    pos = 'after'
  } else if (childful.value.has(n.id)) {
    pos = 'inside'
  } else {
    return
  }
  e.preventDefault()
  e.dataTransfer.dropEffect = 'move'
  dropPos.value = { id: n.id, pos }
}

function onDragLeave(e, n) {
  // 移到行内子元素上也会触发 dragleave：还在这一行里就不清高亮
  if (e.currentTarget.contains(e.relatedTarget)) return
  if (dropPos.value.id === n.id) dropPos.value = { id: '', pos: '' }
}

function nextSiblingOf(n) {
  const parent = n.parent_id || ''
  const siblings = props.tree.filter((x) => (x.parent_id || '') === parent)
  const idx = siblings.findIndex((x) => x.id === n.id)
  return idx >= 0 ? siblings[idx + 1] || null : null
}

function onDrop(e, n) {
  const src = dragNode.value
  if (!src || !dropPos.value.id) return
  e.preventDefault()
  const pos = dropPos.value.pos
  const next = pos === 'after' ? nextSiblingOf(n) : null
  dragNode.value = null
  dropPos.value = { id: '', pos: '' }
  if (pos === 'inside') emit('reparent', src, n, { mode: 'inside' })
  else if (pos === 'before') emit('reparent', src, n, { mode: 'before' })
  else emit('reparent', src, n, { mode: 'after', beforeId: next?.id || '' })
}

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
// 得先按 parent_id 归堆。容器（卷/篇）与叶子分开排：卷只跟卷换，章只跟章换。
const ranks = computed(() => {
  const groups = new Map()
  for (const n of props.tree) {
    const kind = n.ordinal == null ? 'container' : 'leaf'
    const key = `${n.parent_id || ''}|${kind}`
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

// ---- 多选：Ctrl 点增删、Shift 按树序圈一段 ----
function togglePick(id) {
  const cur = new Set(props.selectedIds)
  if (cur.has(id)) cur.delete(id)
  else cur.add(id)
  return [...cur]
}

function rangeTo(id) {
  // Shift 段按扁平树序取，两端都算；没有锚点时等于只选这一个
  const anchor = props.activeId || props.selectedIds[props.selectedIds.length - 1]
  const ids = props.tree.map((x) => x.id)
  const a = anchor ? ids.indexOf(anchor) : -1
  const b = ids.indexOf(id)
  if (a < 0 || b < 0 || a === b) return [id]
  const [lo, hi] = a < b ? [a, b] : [b, a]
  return ids.slice(lo, hi + 1)
}

// ---- 右键菜单 ----
const menu = ref({ node: null, x: 0, y: 0 })

function openMenu(e, n) {
  // 在已选集合里右键：保留多选，菜单里的批量项才有用；否则右键即单选这行
  if (!props.selectedIds.includes(n.id)) emit('update:selected', [n.id])
  menu.value = {
    node: n,
    // 贴边收敛：菜单约 220 宽、最多 12 项，超出视口就换边/上提
    x: Math.max(8, Math.min(e.clientX, window.innerWidth - 236)),
    y: Math.max(8, Math.min(e.clientY, window.innerHeight - 380)),
  }
}

function closeMenu() {
  menu.value = { node: null, x: 0, y: 0 }
}

const containers = computed(() => props.tree.filter((x) => x.ordinal == null))

/** 同一父节点下、树序里紧接其后的叶子兄弟（「与下一节合并」的判据） */
function nextLeafSibling(n) {
  const sibs = props.tree.filter((x) => (x.parent_id || '') === (n.parent_id || ''))
  const i = sibs.findIndex((x) => x.id === n.id)
  const next = i >= 0 ? sibs[i + 1] : null
  return next && next.ordinal != null ? next : null
}

const menuItems = computed(() => {
  const n = menu.value.node
  if (!n) return []
  const isLeaf = n.ordinal != null
  const items = [
    { act: 'create', label: '新建子节点', on: () => emit('create', n) },
  ]
  if (isLeaf) {
    items.push(
      { act: 'insertBefore', label: '在前面插入', on: () => emit('insert', n, 'before') },
      { act: 'insertAfter', label: '在后面插入', on: () => emit('insert', n, 'after') },
      { act: 'up', label: '上移', disabled: rankOf(n.id).first, on: () => emit('move', n, 'up') },
      { act: 'down', label: '下移', disabled: rankOf(n.id).last, on: () => emit('move', n, 'down') },
      { act: 'duplicate', label: '复制（连子树）', on: () => emit('duplicate', n) },
      {
        act: 'split',
        label: '在光标处拆分',
        disabled: n.id !== props.splittableId,
        hint: n.id !== props.splittableId ? '先在正文里放好光标' : '',
        on: () => emit('split', n),
      },
      {
        act: 'merge',
        label: '与下一节合并',
        disabled: !nextLeafSibling(n),
        hint: nextLeafSibling(n) ? '' : '后面没有叶子兄弟',
        on: () => emit('merge', n),
      },
    )
  }
  items.push({ act: 'remove', label: '删除', danger: true, on: () => emit('remove', n) })
  return items
})

function runMenu(it) {
  closeMenu()
  if (!it.disabled) it.on()
}

function emitBatch(act) {
  emit('batch', act, props.selectedIds, { targetId: batchTarget.value })
  batchTarget.value = ''
}
const batchTarget = ref('')

watch(menu, (m) => {
  if (!m.node) return
  const onKey = (e) => {
    if (e.key === 'Escape') {
      closeMenu()
      window.removeEventListener('keydown', onKey)
    }
  }
  window.addEventListener('keydown', onKey)
})

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
/* Ctrl/Shift 圈中的行：描一圈虚线，与「正在编辑」的左侧实条区分开 */
.tree-row.is-picked {
  background: rgba(var(--run-rgb), 0.07);
  box-shadow: inset 0 0 0 1px rgba(var(--run-rgb), 0.35);
}
/* 拖起来的那行淡下去；落点用「上下沿一条线 / 中间描一圈」区分插入与放进 */
.tree-row.is-dragging {
  opacity: 0.42;
}
.tree-row.is-drop-inside {
  background: rgba(var(--run-rgb), 0.12);
  box-shadow: inset 0 0 0 1px rgba(var(--run-rgb), 0.45);
}
.tree-row.is-drop-before::after,
.tree-row.is-drop-after::after {
  content: '';
  position: absolute;
  left: 6px;
  right: 6px;
  height: 2px;
  border-radius: 2px;
  background: var(--run);
  pointer-events: none;
}
.tree-row.is-drop-before::after {
  top: -1px;
}
.tree-row.is-drop-after::after {
  bottom: -1px;
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
/* 多选时替掉统计条：批量动作就摆在这一排，不用回到菜单里一个个点 */
.tree-batch {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px 10px;
  border-top: 1px solid var(--line-soft);
  background: rgba(var(--run-rgb), 0.05);
}
.batch-n {
  flex-shrink: 0;
  font-size: 11px;
  font-family: theme('fontFamily.mono');
  color: var(--run);
}
.batch-sel {
  flex: 1;
  min-width: 0;
  height: 24px;
  border: 1px solid var(--line-soft);
  border-radius: 5px;
  background: var(--panel);
  color: var(--text-muted);
  font-size: 11px;
}
.ctx-mask {
  position: fixed;
  inset: 0;
  z-index: 160;
}
.ctx-menu {
  position: absolute;
  width: 220px;
  padding: 5px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
  box-shadow: 0 18px 44px -16px rgba(var(--shade-rgb), 0.7);
}
.ctx-title {
  margin: 2px 8px 4px;
  padding-bottom: 5px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 11.5px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ctx-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  font-size: 12.5px;
  color: var(--text);
  cursor: pointer;
}
.ctx-item:hover {
  background: rgba(var(--over), 0.07);
}
.ctx-item.is-danger {
  color: var(--danger-fg);
}
.ctx-item.is-off,
.ctx-item:disabled {
  opacity: 0.45;
  cursor: default;
}
.ctx-label {
  flex-shrink: 0;
}
.ctx-hint {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 10.5px;
  color: var(--text-faint);
}
</style>
