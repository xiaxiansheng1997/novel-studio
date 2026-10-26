<template>
  <article class="msg fade-in" :class="message.role">
    <div class="avatar" :class="message.role" aria-hidden="true">
      <span v-if="message.role === 'user'">你</span>
      <AppIcon v-else-if="message.role === 'system'" name="sliders" :size="15" />
      <span v-else class="avatar-ai">AI</span>
    </div>

    <div class="msg-main">
      <header class="msg-head">
        <span class="msg-role">{{ roleLabel }}</span>
        <span v-if="time" class="msg-time">{{ time }}</span>
        <span v-if="streaming" class="msg-live">
          <span class="status-dot on"></span>
          生成中
        </span>
      </header>

      <div v-if="message.reasoning_content" class="reasoning">
        <button class="reasoning-toggle" :aria-expanded="showReasoning" @click="showReasoning = !showReasoning">
          <AppIcon :name="showReasoning ? 'chevronDown' : 'chevronRight'" :size="14" />
          <span>思考过程</span>
        </button>
        <div v-show="showReasoning" class="reasoning-body prose-chat" v-html="renderedReasoning"></div>
      </div>

      <div class="bubble">
        <div class="prose-chat" :class="{ 'typing-cursor': streaming }" v-html="renderedContent"></div>
        <p v-if="!message.content && !streaming" class="bubble-empty">（空回复）</p>
      </div>

      <div v-if="!streaming && (message.content || showRegenerate || showEdit)" class="msg-actions">
        <button v-if="showRegenerate" class="action-btn" title="重新生成这条回答" @click="$emit('regenerate')">
          <AppIcon name="refresh" :size="13" />
          <span>重新生成</span>
        </button>
        <button v-if="showEdit" class="action-btn" title="编辑这条提问并重发" @click="$emit('edit')">
          <AppIcon name="edit" :size="13" />
          <span>编辑</span>
        </button>
        <button v-if="message.content" class="action-btn" :aria-label="copied ? '已复制' : '复制内容'" @click="copyText">
          <AppIcon :name="copied ? 'check' : 'copy'" :size="13" />
          <span>{{ copied ? '已复制' : '复制' }}</span>
        </button>
      </div>
    </div>
  </article>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { marked } from 'marked'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  message: { type: Object, required: true },
  streaming: { type: Boolean, default: false },
  // 只有「最后一条」消息才露出重新生成 / 编辑：中间轮次改了也会被后续回答覆盖
  showRegenerate: { type: Boolean, default: false },
  showEdit: { type: Boolean, default: false },
})
defineEmits(['regenerate', 'edit'])

const showReasoning = ref(false)
const copied = ref(false)

const roleLabel = computed(() => {
  const map = { user: '你', assistant: '助手', system: '系统' }
  return map[props.message.role] || props.message.role
})

const time = computed(() => {
  if (!props.message.created_at) return ''
  return new Date(props.message.created_at * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  })
})

function renderMarkdown(text) {
  if (!text) return ''
  return marked(text, { breaks: true, gfm: true })
}

// 上游偶尔把站点 UI 占位符（如千问 [[inline_action_reply_1]]、[[doc_common_card_1]]）
// 混进正文。后端流式管道已经会剔除，这里兜底处理历史消息与旧数据。
const CONTROL_TOKEN_RE = /\[\[[A-Za-z0-9_.-]{1,64}\]\]/g
// 卡片/建议类标记：容忍全角括号、内部空白与零宽字符（名字里需带 card/reply/action/suggest）
const CARD_TOKEN_RE = /[\[［]{2}[ \t\u200b-\u200f]*(?:[A-Za-z0-9_-]|[ \t\u200b-\u200f])*?(?:card|reply|action|suggest)(?:[A-Za-z0-9_-]|[ \t\u200b-\u200f])*?\d+[ \t\u200b-\u200f]*[\]］]{2}/gi

function stripTokens(text) {
  if (!text) return ''
  const out = text.replace(CONTROL_TOKEN_RE, '').replace(CARD_TOKEN_RE, '')
  // 标记独占一行时，删完会剩三四个换行，压平成一段
  const tidy = out === text ? out : out.replace(/\n{3,}/g, '\n\n')
  return tidy.replace(/\s+$/, '')
}

// 流式时每个 token 都会触发重算；markdown 是 O(n) 且要重建 DOM，
// 这里把「取最新文本」节流到 80ms 一次，流结束后立即同步一次，长回答不再掉帧。
const RENDER_INTERVAL_MS = 80
const contentSource = ref('')
const reasoningSource = ref('')
let renderTimer = 0
let lastRenderAt = 0

function syncSource() {
  renderTimer = 0
  lastRenderAt = performance.now()
  contentSource.value = stripTokens(props.message.content)
  reasoningSource.value = stripTokens(props.message.reasoning_content)
}

function scheduleRender() {
  // 非流式（历史消息、刚结束）直接同步，保证收尾不被延后
  if (!props.streaming) {
    if (renderTimer) {
      clearTimeout(renderTimer)
      renderTimer = 0
    }
    syncSource()
    return
  }
  if (renderTimer) return
  const wait = Math.max(0, RENDER_INTERVAL_MS - (performance.now() - lastRenderAt))
  renderTimer = setTimeout(syncSource, wait)
}

watch(
  () => [props.message.content, props.message.reasoning_content, props.streaming],
  scheduleRender,
  { immediate: true },
)

onBeforeUnmount(() => {
  if (renderTimer) clearTimeout(renderTimer)
})

const renderedContent = computed(() => renderMarkdown(contentSource.value))
const renderedReasoning = computed(() => renderMarkdown(reasoningSource.value))

async function copyText() {
  try {
    await navigator.clipboard.writeText(stripTokens(props.message.content))
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  } catch (_) {
    /* 剪贴板不可用时静默失败 */
  }
}
</script>

<style scoped>
.msg {
  display: flex;
  gap: 12px;
  max-width: 880px;
  margin: 0 auto 22px;
}
.msg.user {
  flex-direction: row-reverse;
}

/* 头像 */
.avatar {
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  margin-top: 2px;
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid var(--line);
  background: #0f1621;
  color: var(--text-muted);
}
.avatar.user {
  border-color: rgba(56, 189, 248, 0.35);
  background: rgba(56, 189, 248, 0.1);
  color: #7dd3fc;
}
.avatar.assistant {
  border-color: rgba(217, 164, 91, 0.35);
  background: rgba(217, 164, 91, 0.1);
  color: #e8c48a;
}
.avatar-ai {
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
  letter-spacing: -0.02em;
}

/* 主体 */
.msg-main {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.msg.user .msg-main {
  align-items: flex-end;
}
.msg-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 2px 5px;
}
.msg-role {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
}
.msg.user .msg-role {
  color: #bae6fd;
}
.msg-time {
  font-size: 11px;
  font-family: theme('fontFamily.mono');
  color: var(--text-faint);
}
.msg-live {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: #e8c48a;
}

/* 气泡 */
.bubble {
  position: relative;
  max-width: 100%;
  padding: 12px 15px;
  border-radius: 4px 14px 14px 14px;
  border: 1px solid var(--line);
  background: #0c121b;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.025);
}
.msg.user .bubble {
  max-width: 82%;
  border-radius: 14px 4px 14px 14px;
  border-color: rgba(56, 189, 248, 0.22);
  background: rgba(56, 189, 248, 0.07);
}
.bubble-empty {
  margin: 0;
  color: var(--text-faint);
  font-size: 13px;
}

/* 思考过程 */
.reasoning {
  width: 100%;
  max-width: 100%;
  margin-bottom: 6px;
}
.reasoning-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px 3px 4px;
  border: 1px dashed var(--line);
  border-radius: 8px;
  background: none;
  color: var(--text-faint);
  font-size: 11.5px;
  cursor: pointer;
  transition: color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.reasoning-toggle:hover {
  color: var(--text-muted);
  border-color: var(--line-strong);
}
.reasoning-body {
  margin-top: 6px;
  padding: 8px 12px;
  border-left: 2px solid var(--line-strong);
  color: var(--text-muted);
  font-size: 13px;
  background: rgba(15, 22, 33, 0.6);
  border-radius: 0 8px 8px 0;
}

/* 操作 */
.msg-actions {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  opacity: 0;
  transition: opacity 0.16s var(--ease);
}
.msg:hover .msg-actions,
.msg-actions:focus-within {
  opacity: 1;
}
.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 9px;
  border-radius: 7px;
  border: 1px solid var(--line-soft);
  background: #0b1018;
  color: var(--text-faint);
  font-size: 11px;
  cursor: pointer;
  transition: color 0.16s var(--ease), border-color 0.16s var(--ease);
}
.action-btn:hover {
  color: var(--text);
  border-color: var(--line-strong);
}

@media (max-width: 720px) {
  .msg {
    gap: 9px;
    margin-bottom: 18px;
  }
  .avatar {
    width: 26px;
    height: 26px;
    border-radius: 8px;
  }
  .bubble {
    padding: 10px 12px;
  }
  .msg.user .bubble {
    max-width: 92%;
  }
}
</style>
