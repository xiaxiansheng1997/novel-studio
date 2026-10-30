<template>
  <div class="ask" :class="{ 'is-shelf': shelf }">
    <header class="ask-top">
      <select
        class="field ask-conv"
        :value="chatStore.activeId || ''"
        aria-label="选择会话"
        @change="onPickConv($event.target.value)"
      >
        <option value="">新对话（未保存）</option>
        <option v-for="c in convOptions" :key="c.id" :value="c.id">{{ c.title || '新对话' }}</option>
      </select>
      <button class="icon-btn" title="新建对话（Ctrl+N）" aria-label="新建对话" @click="newConversation">
        <AppIcon name="plus" :size="15" />
      </button>
      <button
        class="icon-btn"
        title="删除当前会话"
        aria-label="删除当前会话"
        :disabled="!chatStore.activeId"
        @click="removeConversation"
      >
        <AppIcon name="trash" :size="14" />
      </button>
    </header>

    <div ref="listEl" class="ask-list" @click="onContentClick">
      <div v-if="!chatStore.messages.length && !chatStore.streaming" class="ask-empty">
        <p class="ask-empty-t">{{ shelf ? '先把想法聊清楚，再决定怎么写' : '有拿不准的地方，就在这儿问' }}</p>
        <p class="ask-empty-s">
          问完可以把回答存成素材或灵感；勾上「附上本节正文」时，模型能直接看到你正在写的这一段。
        </p>
        <div class="ask-suggests">
          <button v-for="s in suggestions" :key="s" class="ask-suggest" @click="inputRef?.setText(s)">
            <AppIcon name="spark" :size="13" />
            <span>{{ s }}</span>
          </button>
        </div>
      </div>

      <MessageBubble
        v-for="(msg, i) in chatStore.messages"
        :key="i"
        :message="msg"
        :streaming="i === chatStore.messages.length - 1 && chatStore.streaming"
        :show-regenerate="!chatStore.streaming && i === lastAssistantIndex"
        @regenerate="regenerate"
      >
        <template #actions>
          <template v-if="msg.role === 'assistant' && msg.content">
            <button
              v-if="workId"
              class="ask-action"
              title="把这条回答存进本作品的素材库"
              @click="saveAsSource(msg)"
            >
              <AppIcon name="archive" :size="13" />
              <span>存为素材</span>
            </button>
            <button class="ask-action" title="把这条回答的第一句记为灵感" @click="saveAsIdea(msg)">
              <AppIcon name="spark" :size="13" />
              <span>记为灵感</span>
            </button>
            <button v-if="shelf" class="ask-action" title="用它填新建作品的题名与主旨" @click="useAsDraft(msg)">
              <AppIcon name="pen" :size="13" />
              <span>用作题名/前提</span>
            </button>
          </template>
        </template>
      </MessageBubble>

      <p v-if="note" class="ask-note-line" :class="{ bad: noteBad }">{{ note }}</p>
    </div>

    <div class="ask-compose">
      <div class="ask-chips">
        <button
          class="ask-chip"
          :class="{ on: attachNode }"
          :disabled="!nodeContent || !nodeContent.trim()"
          :title="nodeContent && nodeContent.trim() ? '把编辑器里这一节的正文一起发过去' : '这一节还没有正文'"
          @click="attachNode = !attachNode"
        >
          <AppIcon name="file" :size="12" /> 附上本节
        </button>
        <button
          v-if="workId"
          class="ask-chip"
          :class="{ on: attachWork }"
          title="把主旨、摘要与检索到的素材一起发过去（只装配，不调模型）"
          @click="attachWork = !attachWork"
        >
          <AppIcon name="layers" :size="12" /> 附上作品梗概
        </button>
        <span class="ask-hint">{{ modelHint }}</span>
      </div>

      <InputArea
        ref="inputRef"
        :streaming="chatStore.streaming"
        :disabled="!providerStore.selectedModel"
        placeholder="问一句；Enter 发送，Shift+Enter 换行"
        @send="send"
        @stop="chatStore.stopStreaming()"
      >
        <template #toolbar>
          <select
            class="field ask-model"
            :value="providerStore.selectedModel"
            aria-label="模型"
            @change="providerStore.setModel($event.target.value)"
          >
            <optgroup v-for="g in providerStore.modelGroups" :key="g.key" :label="g.name">
              <option v-for="m in g.models" :key="m.id" :value="m.id">{{ m.label }}</option>
            </optgroup>
          </select>
          <button
            v-if="providerStore.supports.thinking"
            class="mode-chip"
            :class="{ on: providerStore.modes.thinking }"
            :disabled="chatStore.streaming"
            title="发送前把网页端切到「深度思考」"
            @click="providerStore.setMode('thinking', !providerStore.modes.thinking)"
          >
            <AppIcon name="spark" :size="12" /> 思考
          </button>
          <button
            v-if="providerStore.supports.search"
            class="mode-chip"
            :class="{ on: providerStore.modes.search }"
            :disabled="chatStore.streaming"
            title="发送前把网页端切到「联网搜索」"
            @click="providerStore.setMode('search', !providerStore.modes.search)"
          >
            <AppIcon name="globe" :size="12" /> 联网
          </button>
        </template>
      </InputArea>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { useProviderStore } from '../stores/provider'
import { useStudioStore } from '../stores/studio'
import { streamChat } from '../utils/sse'
import { getApiKey } from '../utils/storage'
import AppIcon from './AppIcon.vue'
import InputArea from './InputArea.vue'
import MessageBubble from './MessageBubble.vue'

const props = defineProps({
  // shelf = 书架上的「先聊聊」：没有作品、没有本节，只能记灵感 / 回填表单
  shelf: { type: Boolean, default: false },
  workId: { type: String, default: '' },
  workTitle: { type: String, default: '' },
  nodeTitle: { type: String, default: '' },
  // 编辑器里的当前草稿（不是库里的 node.content）：没落盘的改动也要能带过去
  nodeContent: { type: String, default: '' },
  initialConv: { type: String, default: '' },
})
const emit = defineEmits(['use'])

const chatStore = useChatStore()
const providerStore = useProviderStore()
const store = useStudioStore()

const listEl = ref(null)
const inputRef = ref(null)
const attachNode = ref(false)
const attachWork = ref(false)
const note = ref('')
const noteBad = ref(false)

const suggestions = computed(() => {
  if (!props.shelf) {
    return [
      '这段的节奏是不是太慢，能砍哪些句子',
      '这个人物此刻最该隐瞒的是什么',
      '给这一节想三个开头，别用天气',
    ]
  }
  return ['我想写一个关于什么的故事', '这个题材最怕写烂在哪', '帮我找三个不落俗套的开头']
})

const convOptions = computed(() => chatStore.conversations.slice(0, 60))

const modelHint = computed(() => {
  const p = providerStore.currentProvider
  if (!providerStore.selectedModel) return '没有可用模型'
  const tail = providerStore.modes.thinking ? ' · 思考' : ''
  return `${p?.display_name || ''}${tail}`
})

const lastAssistantIndex = computed(() => {
  for (let i = chatStore.messages.length - 1; i >= 0; i--) {
    if (chatStore.messages[i].role === 'assistant') return i
  }
  return -1
})

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  await Promise.all([providerStore.fetchModels(), chatStore.fetchConversations()])
  if (props.initialConv) {
    await chatStore.selectConversation(props.initialConv)
    scrollToBottom()
  }
})
onUnmounted(() => window.removeEventListener('keydown', onKey))

function onKey(e) {
  // 面板里按 Ctrl+N 新开一轮；输入框里打字时也不冲突
  if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
    const el = document.activeElement
    if (el && el.closest && el.closest('.ask')) {
      e.preventDefault()
      newConversation()
    }
  }
}

watch(() => chatStore.messages.length, () => nextTick(scrollToBottom))

let scrollFrame = 0
function scrollToBottom() {
  if (scrollFrame) return
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

function flash(msg, bad = false) {
  note.value = msg
  noteBad.value = bad
  setTimeout(() => {
    if (note.value === msg) note.value = ''
  }, 4000)
}

async function onPickConv(id) {
  if (!id) {
    newConversation()
    return
  }
  chatStore.stopStreaming()
  await chatStore.selectConversation(id)
  scrollToBottom()
}

function newConversation() {
  chatStore.stopStreaming()
  chatStore.activeId = null
  chatStore.messages = []
}

async function removeConversation() {
  const id = chatStore.activeId
  if (!id) return
  if (!window.confirm('删掉这个会话？消息一起消失，不能恢复。')) return
  await chatStore.deleteConversation(id)
}

/**
 * 上下文拼装：附上的内容直接进「用户消息」，用户在气泡里看得见发了什么。
 * 梗概走 /studio/context/preview —— 只装配不调模型，和生成用的是同一份拼装逻辑。
 */
async function compose(text) {
  const blocks = []
  if (attachNode.value && props.nodeContent && props.nodeContent.trim()) {
    blocks.push(`【当前节：${props.nodeTitle || '未命名'}】\n${props.nodeContent.trim()}`)
  }
  if (attachWork.value && props.workId) {
    const ctx = await store.previewContext({ task_type: 'article_draft', instruction: text })
    const body = (ctx?.user || '').trim()
    if (body) {
      const clip = body.length > 1800 ? body.slice(0, 1800) + '\n…（截断）' : body
      blocks.push(`【作品背景】\n${clip}`)
    }
  }
  return blocks.length ? blocks.join('\n\n') + '\n\n' + text : text
}

function convTitle(question) {
  const head = question.replace(/\s+/g, ' ').slice(0, 18)
  // 会话表还没有 work_id（后端本批不动）：标题前缀作品名是最省事的归属线索
  return props.workTitle ? `《${props.workTitle}》· ${head}` : head
}

async function send(raw) {
  if (chatStore.streaming) return
  const model = providerStore.selectedModel
  if (!model) {
    flash('没有可用模型：检查 config.yaml 里的 provider', true)
    return
  }
  let text = raw
  if (attachWork.value || attachNode.value) {
    flash('正在装配上下文…')
    try {
      text = await compose(raw)
    } catch (_) {
      text = raw
    }
  }

  if (!chatStore.activeId) {
    await chatStore.createConversation(model, convTitle(raw))
  }

  chatStore.messages.push({ role: 'user', content: text, created_at: Date.now() / 1000 })
  chatStore.messages.push({ role: 'assistant', content: '', reasoning_content: '', created_at: Date.now() / 1000 })
  const aIdx = chatStore.messages.length - 1
  const apiMessages = chatStore.messages
    .slice(0, aIdx)
    .filter((m) => m.content)
    .map((m) => ({ role: m.role, content: m.content }))

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
      onResponse: (resp) => {
        const id = resp.headers.get('X-W2A-User-Message-Id')
        const um = chatStore.messages[aIdx - 1]
        if (id && um) um.id = id
      },
    })) {
      const cur = chatStore.messages[aIdx]
      if (ev.content) cur.content += ev.content
      if (ev.reasoning_content) cur.reasoning_content += ev.reasoning_content
      scrollToBottom()
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      const cur = chatStore.messages[aIdx]
      const hint = /\b401\b/.test(err.message || '') ? '（服务端设了访问口令：点左下角钥匙填入）' : ''
      cur.content = cur.content || `请求失败：${err.message}${hint}`
    }
  } finally {
    chatStore.streaming = false
    chatStore.abortController = null
    // 标题可能被后端按首条消息改写，刷新一次列表
    await chatStore.fetchConversations()
  }
}

async function regenerate() {
  if (chatStore.streaming) return
  const lastUser = [...chatStore.messages].reverse().find((m) => m.role === 'user')
  if (!lastUser) return
  const model = providerStore.selectedModel
  if (!model || !chatStore.activeId) return

  while (chatStore.messages.length && chatStore.messages[chatStore.messages.length - 1].role !== 'user') {
    chatStore.messages.pop()
  }
  chatStore.messages.push({ role: 'assistant', content: '', reasoning_content: '', created_at: Date.now() / 1000 })
  const aIdx = chatStore.messages.length - 1

  chatStore.streaming = true
  const controller = new AbortController()
  chatStore.abortController = controller
  const modePayload = providerStore.modePayload()
  try {
    for await (const ev of streamChat({
      model,
      messages: chatStore.messages.slice(0, aIdx).filter((m) => m.content).map((m) => ({ role: m.role, content: m.content })),
      signal: controller.signal,
      apiKey: getApiKey(),
      conversationId: chatStore.activeId,
      thinking: modePayload.thinking,
      search: modePayload.search,
      mode: modePayload.mode,
      regenerate: true,
    })) {
      const cur = chatStore.messages[aIdx]
      if (ev.content) cur.content += ev.content
      if (ev.reasoning_content) cur.reasoning_content += ev.reasoning_content
      scrollToBottom()
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      const cur = chatStore.messages[aIdx]
      cur.content = cur.content || `请求失败：${err.message}`
    }
  } finally {
    chatStore.streaming = false
    chatStore.abortController = null
  }
}

// ---- 回答落库：存成素材 / 记成灵感 / 回填新建表单 ----

/** 标题取回答的第一行（去掉 markdown 记号），够看清是哪条 */
function titleOf(text) {
  const first = String(text || '')
    .split('\n')
    .map((s) => s.replace(/^[#>*\-\s]+/, '').trim())
    .find(Boolean)
  return (first || 'AI 回答').slice(0, 40)
}

async function saveAsSource(msg) {
  if (!props.workId) return
  const ok = await store.addSource({
    title: titleOf(msg.content),
    content: msg.content,
    url: '',
    published_at: '',
  })
  flash(ok ? '已存入素材：到「素材」tab 看得到' : store.error || '存入失败', !ok)
}

async function saveAsIdea(msg) {
  const idea = await store.addIdea(titleOf(msg.content))
  flash(idea ? '已记进灵感箱（在书架上方）' : store.error || '记下失败', !idea)
}

function useAsDraft(msg) {
  const text = String(msg.content || '').trim()
  emit('use', { title: titleOf(text).slice(0, 40), premise: text.slice(0, 200) })
}

// 代码块里的「复制」是 v-html 生成的，拿不到组件事件，只能事件委托
function onContentClick(e) {
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
.ask {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.ask.is-shelf {
  background: var(--panel);
}

/* 顶部：会话切换 */
.ask-top {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--line-soft);
  flex-shrink: 0;
}
.ask-conv {
  flex: 1;
  min-width: 0;
  height: 30px;
  font-size: 12px;
}
.ask-top .icon-btn {
  width: 28px;
  height: 28px;
  flex-shrink: 0;
}
.ask-top .icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 消息区 */
.ask-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 10px 4px;
}
.ask-empty {
  padding: 18px 6px;
}
.ask-empty-t {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.ask-empty-s {
  margin: 0 0 12px;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-faint);
}
.ask-suggests {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ask-suggest {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 10px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-control);
  background: var(--panel-2);
  color: var(--text-muted);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.16s var(--ease), color 0.16s var(--ease);
}
.ask-suggest:hover {
  border-color: rgba(var(--run-rgb), 0.4);
  color: var(--text);
}
.ask-note-line {
  margin: 2px 4px 8px;
  font-size: 11.5px;
  color: var(--run-fg);
}
.ask-note-line.bad {
  color: var(--danger-fg);
}

/* 输入区 */
.ask-compose {
  flex-shrink: 0;
  border-top: 1px solid var(--line-soft);
  padding-top: 6px;
  background: var(--panel);
}
.ask-chips {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 12px 0;
  flex-wrap: wrap;
}
.ask-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--line-soft);
  background: var(--panel-2);
  color: var(--text-faint);
  font-size: 11px;
  cursor: pointer;
  transition: border-color 0.16s var(--ease), color 0.16s var(--ease), background-color 0.16s var(--ease);
}
.ask-chip:hover:not(:disabled) {
  border-color: var(--line-strong);
  color: var(--text-muted);
}
.ask-chip.on {
  border-color: rgba(var(--run-rgb), 0.42);
  background: rgba(var(--run-rgb), 0.1);
  color: var(--run-fg);
}
.ask-chip:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.ask-hint {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ask-model {
  width: auto;
  max-width: 190px;
  height: 26px;
  padding: 0 8px;
  font-size: 11.5px;
}
.mode-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 9px;
  border-radius: 999px;
  border: 1px solid var(--line-soft);
  background: var(--panel);
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease), color 0.18s var(--ease);
}
.mode-chip:hover:not(:disabled) {
  border-color: rgba(var(--run-rgb), 0.4);
  color: var(--text);
}
.mode-chip.on {
  border-color: rgba(var(--run-rgb), 0.55);
  background: rgba(var(--run-rgb), 0.14);
  color: var(--run-fg);
}
.mode-chip:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 面板只有五百来像素：气泡里的字体和头像收小一档，别让长回答挤成一条 */
.ask-list :deep(.msg) {
  gap: 8px;
  margin-bottom: 16px;
}
/* 「存为素材 / 记为灵感」是这个面板的主功能之一，不藏进 hover。
   这几个按钮是插槽内容，编译在 AskPanel 作用域里，拿不到 MessageBubble 的 scoped .action-btn，
   所以样式得在这里写一份（外观跟那一套对齐）。 */
.ask-list :deep(.msg-actions) {
  opacity: 1;
}
.ask-action {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  height: 26px;
  padding: 0 9px;
  border-radius: 7px;
  border: 1px solid var(--line-soft);
  background: var(--panel-2);
  color: var(--text-faint);
  font-size: 11px;
  cursor: pointer;
  transition: color 0.16s var(--ease), border-color 0.16s var(--ease);
}
.ask-action:hover {
  color: var(--text);
  border-color: var(--line-strong);
}
.ask-list :deep(.avatar) {
  width: 26px;
  height: 26px;
  border-radius: 8px;
}
.ask-list :deep(.prose-chat) {
  font-size: 13px;
  line-height: 1.68;
}
.ask-list :deep(.msg.user .bubble) {
  max-width: 92%;
}
.ask-compose :deep(.composer-area) {
  padding: 6px 10px 8px;
  background: none;
}
.ask-compose :deep(.composer) {
  border-radius: 12px;
  padding: 6px 6px 6px 11px;
}
.ask-compose :deep(textarea) {
  font-size: 13px;
}
.ask-compose :deep(.composer-hint) {
  display: none;
}
</style>
