<template>
  <div class="composer-area">
    <div class="composer" :class="{ 'is-streaming': streaming }">
      <div class="composer-main">
        <textarea
          ref="textareaEl"
          v-model="text"
          :placeholder="placeholder"
          :disabled="disabled"
          rows="1"
          aria-label="消息输入框"
          @keydown="handleKeydown"
          @input="autoResize"
        ></textarea>

        <div class="composer-actions">
          <button
            v-if="!streaming"
            class="send-btn"
            :disabled="!text.trim() || disabled"
            aria-label="发送消息"
            title="发送 (Enter)"
            @click="doSend"
          >
            <AppIcon name="send" :size="15" />
          </button>
          <button v-else class="stop-btn" aria-label="停止生成" title="停止生成 (Esc)" @click="$emit('stop')">
            <AppIcon name="stop" :size="14" />
          </button>
        </div>
      </div>

      <div v-if="$slots.toolbar" class="composer-toolbar">
        <slot name="toolbar" />
      </div>
    </div>

    <div class="composer-hint">
      <span><span class="kbd">Enter</span> 发送</span>
      <span class="hint-sep">·</span>
      <span><span class="kbd">Shift</span> + <span class="kbd">Enter</span> 换行</span>
      <span class="hint-spacer"></span>
      <span v-if="streaming" class="hint-live"><span class="status-dot on"></span> 正在生成</span>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'
import AppIcon from './AppIcon.vue'

const props = defineProps({
  streaming: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  placeholder: { type: String, default: '输入消息，和网页端对话一样…' },
})
const emit = defineEmits(['send', 'stop'])

const text = ref('')
const textareaEl = ref(null)

function handleKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    doSend()
  }
}

function doSend() {
  const trimmed = text.value.trim()
  if (!trimmed || props.streaming || props.disabled) return
  emit('send', trimmed)
  text.value = ''
  nextTick(() => autoResize())
}

function autoResize() {
  const el = textareaEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

// 供父组件填充建议词
function setText(value) {
  text.value = value || ''
  nextTick(() => {
    autoResize()
    textareaEl.value?.focus()
  })
}

defineExpose({ setText, focus: () => textareaEl.value?.focus() })
</script>

<style scoped>
.composer-area {
  padding: 10px 20px 14px;
  background: linear-gradient(180deg, rgba(11, 12, 15, 0) 0%, #0b0c0f 42%);
}
.composer {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  max-width: 880px;
  margin: 0 auto;
  padding: 8px 8px 8px 14px;
  border-radius: 16px;
  border: 1px solid var(--line);
  background: #0c121b;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03), 0 18px 40px -28px rgba(0, 0, 0, 0.9);
  transition: border-color 0.18s var(--ease), box-shadow 0.18s var(--ease);
}
.composer-main {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
.composer-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 0 2px 1px;
}
.composer:focus-within {
  border-color: rgba(217, 164, 91, 0.55);
  box-shadow: var(--ring), inset 0 1px 0 rgba(255, 255, 255, 0.03);
}
.composer.is-streaming {
  border-color: rgba(217, 164, 91, 0.28);
}
textarea {
  flex: 1;
  min-height: 26px;
  max-height: 200px;
  padding: 4px 0 3px;
  border: none;
  background: none;
  color: var(--text);
  font: inherit;
  font-size: 14px;
  line-height: 1.55;
  resize: none;
  outline: none;
}
textarea::placeholder {
  color: var(--text-faint);
}
.composer-actions {
  display: flex;
  gap: 6px;
}
.send-btn,
.stop-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  border: 1px solid transparent;
  cursor: pointer;
  transition: background-color 0.18s var(--ease), color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.send-btn {
  background: var(--run);
  color: #04120a;
}
.send-btn:hover {
  background: var(--run-dim);
  color: #fff;
}
.send-btn:disabled {
  background: #16202d;
  color: #4d5b70;
  cursor: not-allowed;
}
.stop-btn {
  border-color: rgba(248, 113, 113, 0.45);
  background: rgba(248, 113, 113, 0.12);
  color: #fca5a5;
}
.stop-btn:hover {
  background: rgba(248, 113, 113, 0.2);
  color: #fecaca;
}
.composer-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 880px;
  margin: 7px auto 0;
  font-size: 11px;
  color: var(--text-faint);
}
.hint-sep {
  opacity: 0.5;
}
.hint-spacer {
  flex: 1;
}
.hint-live {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #e8c48a;
}
@media (max-width: 720px) {
  .composer-area {
    padding: 8px 12px 10px;
  }
  .composer-hint {
    display: none;
  }
}
</style>
