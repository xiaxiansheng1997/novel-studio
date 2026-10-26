<template>
  <div class="app-shell">
    <AppNav />
    <Sidebar
      :conversations="chatStore.conversations"
      :active-id="chatStore.activeId"
      :collapsed="sidebarCollapsed"
      @new-chat="handleNewChat"
      @select="handleSelect"
      @delete="handleDelete"
      @rename="handleRename"
    />
    <div v-if="!sidebarCollapsed" class="sidebar-backdrop" @click="sidebarCollapsed = true"></div>

    <main id="main" class="chat-main">
      <header class="topbar">
        <button
          class="icon-btn"
          :aria-label="sidebarCollapsed ? '展开会话列表' : '收起会话列表'"
          :aria-expanded="!sidebarCollapsed"
          @click="sidebarCollapsed = !sidebarCollapsed"
        >
          <AppIcon name="panel" :size="18" />
        </button>

        <div class="topbar-title">
          <h1>{{ activeTitle }}</h1>
          <span class="topbar-sub">{{ chatStore.messages.length }} 条消息</span>
        </div>

        <div class="topbar-right">
          <button
            class="icon-btn"
            title="导出当前会话为 Markdown"
            aria-label="导出当前会话"
            :disabled="!chatStore.activeId"
            @click="exportActive"
          >
            <AppIcon name="download" :size="17" />
          </button>
          <label class="model-field">
            <span class="model-label">模型</span>
            <select class="field model-select" :value="providerStore.selectedModel" @change="onModelChange">
              <optgroup v-for="g in providerStore.modelGroups" :key="g.key" :label="g.name">
                <option v-for="m in g.models" :key="m.id" :value="m.id">{{ m.label }} · {{ m.id }}</option>
              </optgroup>
            </select>
          </label>
          <span class="pill" :class="providerStatusPill">
            <span class="status-dot" :class="providerStatusDot"></span>
            {{ providerStatusText }}
          </span>
        </div>
      </header>

      <div ref="messagesEl" class="messages" @click="handleContentClick">
        <div v-if="!chatStore.messages.length && !chatStore.streaming" class="empty">
          <div class="empty-mark" aria-hidden="true">
            <AppIcon name="terminal" :size="22" />
          </div>
          <h2>开始一段新对话</h2>
          <p class="empty-sub">选择一个模型，直接输入问题。回复会实时流式返回。</p>

          <div class="suggests">
            <button v-for="s in suggestions" :key="s.text" class="suggest" @click="useSuggestion(s.text)">
              <AppIcon :name="s.icon" :size="15" />
              <span>{{ s.text }}</span>
              <AppIcon name="chevronRight" :size="14" class="suggest-arrow" />
            </button>
          </div>
        </div>

        <MessageBubble
          v-for="(msg, i) in chatStore.messages"
          :key="i"
          :message="msg"
          :streaming="i === chatStore.messages.length - 1 && chatStore.streaming"
          :show-regenerate="!chatStore.streaming && i === lastAssistantIndex"
          :show-edit="!chatStore.streaming && i === lastUserIndex"
          @regenerate="regenerate"
          @edit="editMessage(i)"
        />
      </div>

      <InputArea
        ref="inputRef"
        :streaming="chatStore.streaming"
        :placeholder="editing ? '编辑这条提问后回车重发（会删掉它后面的回答）…' : '输入消息，和网页端对话一样…'"
        @send="handleSend"
        @stop="chatStore.stopStreaming()"
      >
        <template #toolbar>
          <button
            class="mode-chip"
            :class="{ on: providerStore.modes.thinking }"
            :disabled="!providerStore.supports.thinking || chatStore.streaming"
            :title="providerStore.supports.thinking ? '发送前把网页端切到「深度思考」' : '当前 provider 没有深度思考开关'"
            @click="toggleMode('thinking')"
          >
            <AppIcon name="spark" :size="12" /> 深度思考
          </button>
          <button
            class="mode-chip"
            :class="{ on: providerStore.modes.search }"
            :disabled="!providerStore.supports.search || chatStore.streaming"
            :title="providerStore.supports.search ? '发送前把网页端切到「联网搜索」' : '当前 provider 没有联网搜索开关'"
            @click="toggleMode('search')"
          >
            <AppIcon name="activity" :size="12" /> 联网
          </button>
          <button
            class="mode-chip"
            :class="{ on: providerStore.modes.expert }"
            :disabled="!providerStore.supports.thinking || !providerStore.supports.search || chatStore.streaming"
            title="专家模式：深度思考 + 联网一起开"
            @click="toggleMode('expert')"
          >
            <AppIcon name="terminal" :size="12" /> 专家模式
          </button>
          <span class="mode-note">{{ modeNote }}</span>
          <button v-if="editing" class="mode-chip editing" title="取消编辑" @click="cancelEdit">
            <AppIcon name="x" :size="12" /> 取消编辑
          </button>
        </template>
      </InputArea>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { useProviderStore } from '../stores/provider'
import { streamChat } from '../utils/sse'
import { getApiKey } from '../utils/storage'
import AppNav from '../components/AppNav.vue'
import AppIcon from '../components/AppIcon.vue'
import Sidebar from '../components/Sidebar.vue'
import MessageBubble from '../components/MessageBubble.vue'
import InputArea from '../components/InputArea.vue'

const chatStore = useChatStore()
const providerStore = useProviderStore()
const messagesEl = ref(null)
const inputRef = ref(null)
const sidebarCollapsed = ref(window.innerWidth <= 1024)
// 编辑重发：{ index, id, text }；id 为空表示这条提问还没拿到服务端 id
const editing = ref(null)

const suggestions = [
  { icon: 'spark', text: '用一句话解释什么是光合作用' },
  { icon: 'terminal', text: '帮我写一个 Python 快速排序' },
  { icon: 'activity', text: '把这段需求拆成开发任务清单' },
]

const activeTitle = computed(() => {
  const conv = chatStore.conversations.find(c => c.id === chatStore.activeId)
  return conv?.title || '新对话'
})

const providerStatusDot = computed(() => {
  const s = providerStore.status
  if (!s.length) return 'unknown'
  return s.some(p => p.has_profile) ? 'on' : 'off'
})
const providerStatusPill = computed(() => {
  const s = providerStore.status
  if (!s.length) return 'pill--off'
  return s.some(p => p.has_profile) ? 'pill--on' : 'pill--warn'
})
const providerStatusText = computed(() => {
  const s = providerStore.status
  if (!s.length) return '未检测'
  const online = s.filter(p => p.has_profile).length
  return `${online}/${s.length} 就绪`
})

onMounted(async () => {
  await providerStore.fetchModels()
  await providerStore.fetchStatus()
  await chatStore.fetchConversations()
  window.addEventListener('keydown', globalKeyHandler)
})

onUnmounted(() => {
  window.removeEventListener('keydown', globalKeyHandler)
  if (scrollFrame) cancelAnimationFrame(scrollFrame)
})

function globalKeyHandler(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
    e.preventDefault()
    handleNewChat()
  }
  if (e.key === 'Escape' && chatStore.streaming) {
    chatStore.stopStreaming()
  } else if (e.key === 'Escape' && editing.value) {
    cancelEdit()
  }
}

watch(() => chatStore.messages.length, () => {
  nextTick(() => scrollToBottom())
})

// 流式时每个 token 都会调一次滚动；用 rAF 合并成每帧最多一次，
// 避免同步读取 scrollHeight 强制重排导致掉帧。
let scrollFrame = 0
function scrollToBottom() {
  if (scrollFrame) return
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0
    if (messagesEl.value) messagesEl.value.scrollTop = messagesEl.value.scrollHeight
  })
}

function onModelChange(e) {
  providerStore.setModel(e.target.value)
}

function toggleMode(key) {
  providerStore.setMode(key, !providerStore.modes[key])
}

const modeNote = computed(() => {
  const p = providerStore.currentProvider
  if (!p) return ''
  const on = []
  if (providerStore.modes.thinking) on.push('思考')
  if (providerStore.modes.search) on.push('联网')
  const tail = on.length ? `· ${on.join(' + ')}` : '· 快速'
  return `${p.display_name} ${tail}`
})

function useSuggestion(text) {
  inputRef.value?.setText(text)
}

async function handleNewChat() {
  if (!providerStore.selectedModel) {
    await providerStore.fetchModels()
  }
  chatStore.activeId = null
  chatStore.messages = []
  editing.value = null
  if (window.innerWidth <= 1024) sidebarCollapsed.value = true
}

async function handleSelect(id) {
  editing.value = null
  await chatStore.selectConversation(id)
  nextTick(() => scrollToBottom())
  if (window.innerWidth <= 1024) sidebarCollapsed.value = true
}

async function handleDelete(id) {
  await chatStore.deleteConversation(id)
}

async function handleSend(text) {
  // 编辑模式下发送：先让后端把旧提问及其后的回答删掉，再当普通消息重发
  const pending = editing.value
  if (pending) {
    editing.value = null
    if (pending.id) await chatStore.rewindConversation(chatStore.activeId, pending.id)
    else await chatStore.rewindConversation(chatStore.activeId)
    chatStore.messages.splice(pending.index)
  }
  await sendText(text)
}

const lastUserIndex = computed(() => {
  for (let i = chatStore.messages.length - 1; i >= 0; i--) {
    if (chatStore.messages[i].role === 'user') return i
  }
  return -1
})
const lastAssistantIndex = computed(() => {
  for (let i = chatStore.messages.length - 1; i >= 0; i--) {
    if (chatStore.messages[i].role === 'assistant') return i
  }
  return -1
})

function cancelEdit() {
  editing.value = null
  inputRef.value?.setText('')
}

function editMessage(idx) {
  const m = chatStore.messages[idx]
  if (!m || chatStore.streaming) return
  editing.value = { index: idx, id: m.id || '', text: m.content }
  inputRef.value?.setText(m.content)
}

async function regenerate() {
  if (chatStore.streaming) return
  const lastUser = [...chatStore.messages].reverse().find(m => m.role === 'user')
  if (!lastUser) return
  await sendText(String(lastUser.content || ''), { regenerate: true })
}

async function sendText(text, { regenerate = false } = {}) {
  const model = providerStore.selectedModel
  if (!model) return

  // Create conversation on first message if none active
  if (!chatStore.activeId) {
    await chatStore.createConversation(model)
  }

  let userMsg = null
  if (regenerate) {
    // 最后一条用户消息留原地，只摘掉它后面的旧回答（后端也会丢掉并重跑）
    while (chatStore.messages.length && chatStore.messages[chatStore.messages.length - 1].role !== 'user') {
      chatStore.messages.pop()
    }
    userMsg = chatStore.messages[chatStore.messages.length - 1] || null
  } else {
    userMsg = { role: 'user', content: text, created_at: Date.now() / 1000 }
    chatStore.messages.push(userMsg)
  }

  // Assistant placeholder: 保留索引，后续通过 chatStore.messages[idx] 访问响应式代理
  chatStore.messages.push({ role: 'assistant', content: '', reasoning_content: '', created_at: Date.now() / 1000 })
  const aIdx = chatStore.messages.length - 1
  // 排除刚插入的空占位；空内容（如被中断的旧回答）不发回上游
  const apiMessages = chatStore.messages
    .slice(0, aIdx)
    .filter(m => m.content)
    .map(m => ({ role: m.role, content: m.content }))

  chatStore.streaming = true
  const controller = new AbortController()
  chatStore.abortController = controller
  const modePayload = providerStore.modePayload()

  try {
    for await (const ev of streamChat({
      model,
      messages: apiMessages,
      signal: controller.signal,
      apiKey: getApiKey(),
      conversationId: chatStore.activeId,
      thinking: modePayload.thinking,
      search: modePayload.search,
      mode: modePayload.mode,
      regenerate,
      onResponse: (resp) => {
        // 后端落库后的消息 id：编辑重发要按它删旧提问
        const id = resp.headers.get('X-W2A-User-Message-Id')
        if (id && userMsg) userMsg.id = id
      },
    })) {
      const cur = chatStore.messages[aIdx] // 通过响应式数组取值，改动才会触发重渲染
      if (ev.content) cur.content += ev.content
      if (ev.reasoning_content) cur.reasoning_content += ev.reasoning_content
      scrollToBottom()
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      const cur = chatStore.messages[aIdx]
      // 401 单独提示：服务端设了 W2A_API_KEY 而前端还没填，不填的话每轮都是这个错
      const hint = /\b401\b/.test(err.message || '') ? '（服务端设了访问口令：点左下角钥匙填入）' : ''
      cur.content = cur.content || `请求失败：${err.message}${hint}`
    }
  } finally {
    chatStore.streaming = false
    chatStore.abortController = null
    await chatStore.fetchConversations()
  }
}

async function exportActive() {
  if (!chatStore.activeId) return
  const j = await chatStore.exportConversation(chatStore.activeId)
  if (!j) return
  const blob = new Blob([j.content || ''], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(activeTitle.value || 'conversation').replace(/[\\/:*?"<>|]/g, '_')}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

async function handleRename(payload) {
  if (!payload?.id || !payload?.title) return
  await chatStore.renameConversation(payload.id, payload.title)
}

// Event delegation for code copy buttons
function handleContentClick(e) {
  const btn = e.target.closest('.copy-btn')
  if (!btn) return
  const code = btn.closest('.code-block-wrap')?.querySelector('code')?.textContent
  if (code) {
    navigator.clipboard.writeText(code)
    btn.textContent = '已复制'
    setTimeout(() => (btn.textContent = '复制'), 1500)
  }
}
</script>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  /* 兜住 dvh：它算错时整块只剩上半截。100% 走 main.css 的 html/body/#app 高链 */
  height: 100%;
  overflow: hidden;
  background: radial-gradient(1200px 600px at 70% -10%, rgba(217, 164, 91, 0.06), transparent 60%),
    #0b0c0f;
}
.chat-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

/* 顶栏 */
.topbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 18px;
  border-bottom: 1px solid var(--line-soft);
  background: rgba(10, 14, 22, 0.85);
  backdrop-filter: blur(10px);
}
.topbar-title {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.topbar-title h1 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.topbar-sub {
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
}
.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
}
.model-field {
  display: flex;
  align-items: center;
  gap: 7px;
}
.model-label {
  font-size: 11px;
  color: var(--text-faint);
}
.model-select {
  width: auto;
  min-width: 180px;
  max-width: 240px;
  height: 32px;
  font-family: theme('fontFamily.mono');
  font-size: 12px;
}

/* 输入框下方的模式开关 */
.mode-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  border: 1px solid var(--line-soft);
  background: #0b1018;
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease), color 0.18s var(--ease);
}
.mode-chip:hover:not(:disabled) {
  border-color: rgba(217, 164, 91, 0.4);
  color: var(--text);
}
.mode-chip.on {
  border-color: rgba(217, 164, 91, 0.55);
  background: rgba(217, 164, 91, 0.14);
  color: #e8c48a;
}
.mode-chip:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
/* 编辑重发中的提示 chip：醒目的黄色，和普通开关区分 */
.mode-chip.editing {
  border-color: rgba(250, 204, 21, 0.5);
  background: rgba(250, 204, 21, 0.12);
  color: #fde68a;
}
.mode-chip.editing:hover:not(:disabled) {
  border-color: rgba(250, 204, 21, 0.75);
  color: #fef3c7;
}
.mode-note {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
}

/* 消息区 */
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 26px 20px 8px;
  scroll-behavior: smooth;
}

/* 空态 */
.empty {
  max-width: 640px;
  margin: 6vh auto 0;
  text-align: center;
}
.empty-mark {
  width: 46px;
  height: 46px;
  margin: 0 auto 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 13px;
  border: 1px solid rgba(217, 164, 91, 0.35);
  background: rgba(217, 164, 91, 0.1);
  color: #e8c48a;
}
.empty h2 {
  margin: 0 0 6px;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.02em;
}
.empty-sub {
  margin: 0 0 22px;
  color: var(--text-faint);
  font-size: 13px;
  text-wrap: balance;
}
.suggests {
  display: grid;
  gap: 8px;
  text-align: left;
}
.suggest {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 11px 14px;
  border-radius: 12px;
  border: 1px solid var(--line-soft);
  background: #0b1018;
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease), color 0.18s var(--ease);
}
.suggest:hover {
  border-color: rgba(217, 164, 91, 0.4);
  background: rgba(217, 164, 91, 0.06);
  color: var(--text);
}
.suggest-arrow {
  margin-left: auto;
  opacity: 0.5;
}

/* 移动端抽屉遮罩 */
.sidebar-backdrop {
  display: none;
}
@media (max-width: 1024px) {
  .sidebar-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 30;
    background: rgba(3, 6, 10, 0.6);
    backdrop-filter: blur(2px);
  }
  .topbar {
    padding: 9px 12px;
  }
  .model-field .model-label {
    display: none;
  }
  .model-select {
    min-width: 0;
    max-width: 150px;
  }
  .messages {
    padding: 18px 14px 6px;
  }
}
@media (max-width: 560px) {
  /* 窄屏把模型选择放到第二行，标题与状态各占一行，避免标题被挤成省略号 */
  .topbar {
    flex-wrap: wrap;
    row-gap: 8px;
  }
  .mode-note {
    display: none;
  }
  .topbar-title {
    flex: 1;
  }
  .topbar-right {
    flex: 1 0 100%;
    margin-left: 0;
    justify-content: space-between;
    gap: 8px;
  }
  .model-field {
    flex: 1;
    min-width: 0;
  }
  .model-select {
    width: 100%;
    max-width: none;
  }
}
</style>
