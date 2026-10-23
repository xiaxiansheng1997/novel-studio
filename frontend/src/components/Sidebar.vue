<template>
  <aside class="sidebar" :class="{ collapsed }" aria-label="会话列表">
    <div class="sidebar-head">
      <button class="new-chat-btn" @click="$emit('new-chat')">
        <AppIcon name="plus" :size="16" />
        <span>新对话</span>
        <span class="kbd new-chat-kbd">Ctrl N</span>
      </button>
    </div>

    <div class="search-box">
      <AppIcon name="search" :size="15" class="search-icon" />
      <input v-model="search" class="search-input" placeholder="搜索会话或消息" aria-label="搜索会话或消息" />
      <button v-if="search" class="search-clear" aria-label="清空搜索" @click="search = ''">
        <AppIcon name="x" :size="13" />
      </button>
    </div>

    <div class="conv-list">
      <div v-if="search && messageHits.length" class="hit-group">
        <div class="conv-group-label">
          <span>消息命中</span>
          <span class="conv-count">{{ messageHits.length }}</span>
        </div>
        <button
          v-for="hit in messageHits"
          :key="hit.message_id"
          class="hit-item"
          :title="hit.title || '新对话'"
          @click="openHit(hit)"
        >
          <span class="hit-title">{{ hit.title || '新对话' }}</span>
          <span class="hit-snippet">{{ hit.snippet }}</span>
        </button>
      </div>

      <div class="conv-group-label">
        <span>{{ search ? '匹配会话' : '最近对话' }}</span>
        <span class="conv-count">{{ filteredConversations.length }}</span>
      </div>

      <div
        v-for="conv in filteredConversations"
        :key="conv.id"
        class="conv-item"
        :class="{ active: conv.id === activeId }"
      >
        <div v-if="renamingId === conv.id" class="conv-rename">
          <input
            v-model="renameText"
            class="rename-input"
            maxlength="60"
            aria-label="重命名会话"
            @keydown.enter.prevent="commitRename(conv)"
            @keydown.esc.prevent="cancelRename"
            @blur="commitRename(conv)"
            @click.stop
          />
        </div>
        <template v-else>
          <button class="conv-main" @click="$emit('select', conv.id)">
            <span class="conv-title">{{ conv.title || '新对话' }}</span>
            <span class="conv-meta">
              <span class="conv-provider">{{ conv.provider || conv.model || '未知模型' }}</span>
              <span v-if="conv.updated_at" class="conv-time">{{ formatTime(conv.updated_at) }}</span>
            </span>
          </button>
          <button class="conv-rename-btn" title="重命名会话" aria-label="重命名会话" @click.stop="startRename(conv)">
            <AppIcon name="edit" :size="13" />
          </button>
          <button class="conv-delete" title="删除会话" aria-label="删除会话" @click.stop="$emit('delete', conv.id)">
            <AppIcon name="trash" :size="14" />
          </button>
        </template>
      </div>

      <div v-if="!filteredConversations.length && !messageHits.length" class="empty-hint">
        <AppIcon name="chat" :size="20" />
        <span>{{ search ? '没有匹配的会话或消息' : '还没有对话，发一条消息就会出现在这里' }}</span>
      </div>
    </div>

    <div class="sidebar-foot">
      <span class="foot-badge">
        <span class="status-dot on"></span>
        OpenAI 兼容
      </span>
      <span class="foot-path">/v1</span>
    </div>
  </aside>
</template>

<script setup>
import { ref, computed, watch, onUnmounted, nextTick } from 'vue'
import { useChatStore } from '../stores/chat'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  conversations: { type: Array, default: () => [] },
  activeId: { type: String, default: null },
  collapsed: { type: Boolean, default: false },
})
const emit = defineEmits(['new-chat', 'select', 'delete', 'rename', 'toggle'])

const chatStore = useChatStore()

const search = ref('')
const filteredConversations = computed(() => {
  const list = props.conversations || []
  if (!search.value) return list
  const q = search.value.toLowerCase()
  return list.filter(c => (c.title || '').toLowerCase().includes(q))
})

// 消息全文搜索：标题过滤在本地做，正文命中走后端 /v1/search（防抖 300ms）
const messageHits = ref([])
let hitsTimer = 0
watch(search, q => {
  clearTimeout(hitsTimer)
  const kw = (q || '').trim()
  if (kw.length < 2) {
    messageHits.value = []
    return
  }
  hitsTimer = setTimeout(async () => {
    messageHits.value = await chatStore.searchMessages(kw)
  }, 300)
})
onUnmounted(() => clearTimeout(hitsTimer))

function openHit(hit) {
  emit('select', hit.conversation_id)
}

// 行内重命名
const renamingId = ref(null)
const renameText = ref('')

async function startRename(conv) {
  renamingId.value = conv.id
  renameText.value = conv.title || ''
  await nextTick()
  const el = document.querySelector('.rename-input')
  el?.focus()
  el?.select()
}

function cancelRename() {
  renamingId.value = null
}

function commitRename(conv) {
  // Enter 提交后 blur 还会再进一次，用 renamingId 守卫
  if (renamingId.value !== conv.id) return
  const title = renameText.value.trim()
  renamingId.value = null
  if (!title || title === (conv.title || '')) return
  emit('rename', { id: conv.id, title })
}

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return `${d.getMonth() + 1}/${d.getDate()}`
}
</script>

<style scoped>
.sidebar {
  width: 272px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: #0a0e16;
  border-right: 1px solid var(--line-soft);
  transition: width 0.22s var(--ease), transform 0.22s var(--ease);
  overflow: hidden;
}
.sidebar.collapsed {
  width: 0;
  border-right: none;
}

/* 头部：主操作 */
.sidebar-head {
  padding: 14px 12px 10px;
}
.new-chat-btn {
  width: 100%;
  height: 38px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  border-radius: 10px;
  border: 1px solid rgba(34, 197, 94, 0.4);
  background: rgba(34, 197, 94, 0.1);
  color: #86efac;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background-color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.new-chat-btn:hover {
  background: rgba(34, 197, 94, 0.16);
  border-color: rgba(34, 197, 94, 0.6);
}
.new-chat-kbd {
  margin-left: auto;
  font-size: 10px;
  opacity: 0.7;
}

/* 搜索 */
.search-box {
  position: relative;
  display: flex;
  align-items: center;
  margin: 0 12px 8px;
}
.search-icon {
  position: absolute;
  left: 10px;
  color: var(--text-faint);
  pointer-events: none;
}
.search-input {
  width: 100%;
  height: 32px;
  padding: 0 28px 0 32px;
  border-radius: 9px;
  border: 1px solid var(--line-soft);
  background: #0d131d;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
  transition: border-color 0.18s var(--ease);
}
.search-input::placeholder {
  color: var(--text-faint);
}
.search-input:focus {
  border-color: var(--line-strong);
}
.search-clear {
  position: absolute;
  right: 8px;
  display: inline-flex;
  padding: 2px;
  border: none;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
}
.search-clear:hover {
  color: var(--text);
}

/* 列表 */
.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px 12px;
}
.conv-group-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 8px 6px;
  font-size: 10.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-faint);
}
.conv-count {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
}
.conv-item {
  position: relative;
  display: flex;
  align-items: stretch;
  border-radius: 10px;
  margin-bottom: 2px;
  transition: background-color 0.16s var(--ease);
}
.conv-item:hover {
  background: #101722;
}
.conv-item.active {
  background: rgba(34, 197, 94, 0.08);
}
.conv-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 2px;
  border-radius: 2px;
  background: var(--run);
}
.conv-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 9px 8px 9px 12px;
  border: none;
  background: none;
  text-align: left;
  cursor: pointer;
}
.conv-title {
  font-size: 13px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-item.active .conv-title {
  color: #d9fbe4;
}
.conv-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--text-faint);
}
.conv-provider {
  font-family: theme('fontFamily.mono');
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-time {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
}
.conv-delete {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  border: none;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s var(--ease), color 0.15s var(--ease);
}
.conv-item:hover .conv-delete,
.conv-delete:focus-visible {
  opacity: 1;
}
.conv-delete:hover {
  color: var(--danger);
}

/* 行内重命名 */
.conv-rename-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  border: none;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s var(--ease), color 0.15s var(--ease);
}
.conv-item:hover .conv-rename-btn,
.conv-rename-btn:focus-visible {
  opacity: 1;
}
.conv-rename-btn:hover {
  color: var(--text);
}
.conv-rename {
  flex: 1;
  min-width: 0;
  padding: 7px 10px;
}
.rename-input {
  width: 100%;
  height: 28px;
  padding: 0 8px;
  border-radius: 7px;
  border: 1px solid rgba(34, 197, 94, 0.55);
  background: #0d131d;
  color: var(--text);
  font-size: 13px;
  outline: none;
}

/* 消息命中 */
.hit-group {
  padding-bottom: 6px;
  margin-bottom: 4px;
  border-bottom: 1px dashed var(--line-soft);
}
.hit-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 7px 10px;
  border: none;
  border-radius: 8px;
  background: none;
  text-align: left;
  cursor: pointer;
  transition: background-color 0.16s var(--ease);
}
.hit-item:hover {
  background: #101722;
}
.hit-title {
  font-size: 12px;
  color: #86efac;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hit-snippet {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-faint);
  word-break: break-word;
}

/* 空态 */
.empty-hint {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 28px 16px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}

/* 底部 */
.sidebar-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-top: 1px solid var(--line-soft);
  font-size: 11px;
  color: var(--text-faint);
}
.foot-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.foot-path {
  font-family: theme('fontFamily.mono');
  padding: 1px 6px;
  border-radius: 5px;
  border: 1px solid var(--line-soft);
  background: #0d131d;
}

@media (max-width: 1024px) {
  .sidebar {
    position: fixed;
    left: 76px;
    top: 0;
    bottom: 0;
    z-index: 40;
    width: 272px;
    box-shadow: 20px 0 48px -24px rgba(0, 0, 0, 0.9);
    visibility: visible;
    transition: transform 0.22s var(--ease);
  }
  /* 侧栏起点在导航栏右侧，收起时要多移出导航栏宽度，否则会残留一条并盖住导航 */
  .sidebar.collapsed {
    transform: translateX(calc(-100% - 76px));
    visibility: hidden;
    transition: transform 0.22s var(--ease), visibility 0s linear 0.22s;
  }
}
@media (max-width: 720px) {
  .sidebar {
    left: 60px;
  }
  .sidebar.collapsed {
    transform: translateX(calc(-100% - 60px));
  }
}
</style>
