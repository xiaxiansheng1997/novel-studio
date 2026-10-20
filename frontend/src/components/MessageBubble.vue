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

      <div v-if="!streaming && message.content" class="msg-actions">
        <button class="action-btn" :aria-label="copied ? '已复制' : '复制内容'" @click="copyText">
          <AppIcon :name="copied ? 'check' : 'copy'" :size="13" />
          <span>{{ copied ? '已复制' : '复制' }}</span>
        </button>
      </div>
    </div>
  </article>
</template>

<script setup>
import { computed, ref } from 'vue'
import { marked } from 'marked'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  message: { type: Object, required: true },
  streaming: { type: Boolean, default: false },
})

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

const renderedContent = computed(() => renderMarkdown(props.message.content))
const renderedReasoning = computed(() => renderMarkdown(props.message.reasoning_content))

async function copyText() {
  try {
    await navigator.clipboard.writeText(props.message.content)
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
  border-color: rgba(34, 197, 94, 0.35);
  background: rgba(34, 197, 94, 0.1);
  color: #86efac;
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
  color: #86efac;
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
