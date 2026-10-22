<template>
  <Teleport to="body">
    <div v-if="open" class="key-mask" @mousedown.self="close">
      <section class="key-box" role="dialog" aria-modal="true" aria-labelledby="key-title">
        <header class="key-head">
          <h2 id="key-title">访问口令</h2>
          <button class="key-x" type="button" aria-label="关闭" @click="close">
            <AppIcon name="x" :size="16" />
          </button>
        </header>

        <p class="key-hint">
          服务端设置了 <code>W2A_API_KEY</code> 时填写；保存后所有
          <code>/v1</code>、<code>/admin</code>、<code>/live</code> 请求都会带上
          <code>Authorization: Bearer</code>。服务端没设口令就留空。
        </p>

        <label class="key-field">
          <span>API Key</span>
          <input
            ref="inputEl"
            v-model="draft"
            type="password"
            autocomplete="off"
            spellcheck="false"
            placeholder="粘贴 W2A_API_KEY；未设置可留空"
            @keydown.enter="save"
          />
        </label>

        <p v-if="msg" class="key-msg" :class="{ 'is-bad': msgBad }">{{ msg }}</p>

        <footer class="key-foot">
          <button class="key-btn ghost" type="button" :disabled="busy" @click="clearKey">清除</button>
          <span class="key-spacer"></span>
          <button class="key-btn ghost" type="button" :disabled="busy" @click="close">取消</button>
          <button class="key-btn primary" type="button" :disabled="busy" @click="save">
            {{ busy ? '校验中…' : '保存并重载' }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { getApiKey, setApiKey } from '../utils/storage'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['update:open'])

const draft = ref('')
const msg = ref('')
const msgBad = ref(false)
const busy = ref(false)
const inputEl = ref(null)

watch(
  () => props.open,
  async (v) => {
    if (!v) return
    draft.value = getApiKey()
    msg.value = ''
    msgBad.value = false
    await nextTick()
    inputEl.value?.focus()
  },
)

function close() {
  if (!busy.value) emit('update:open', false)
}

async function save() {
  const key = draft.value.trim()
  // 清空口令不需要校验（空串访问受保护接口必然 401，拿它当“口令不对”是误报）
  if (!key) {
    clearKey()
    return
  }
  busy.value = true
  msg.value = ''
  msgBad.value = false
  try {
    // 先用受保护的模型列表验一次：口令错误时当场提示，避免保存后整页 401
    const r = await fetch('/v1/models', {
      headers: { Authorization: 'Bearer ' + key },
    })
    if (r.status === 401) {
      msgBad.value = true
      msg.value = '口令不正确，服务端拒绝了这次请求'
      return
    }
    if (!r.ok) {
      msgBad.value = true
      msg.value = '校验失败：HTTP ' + r.status
      return
    }
    setApiKey(key)
    msg.value = '已保存，正在重载…'
    setTimeout(() => window.location.reload(), 350)
  } catch (e) {
    msgBad.value = true
    msg.value = '无法连接服务端：' + e.message
  } finally {
    busy.value = false
  }
}

function clearKey() {
  setApiKey('')
  draft.value = ''
  msgBad.value = false
  msg.value = '已清空口令，正在重载…'
  setTimeout(() => window.location.reload(), 350)
}
</script>

<style scoped>
.key-mask {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(3, 6, 11, 0.68);
  backdrop-filter: blur(3px);
}
.key-box {
  width: min(460px, 100%);
  padding: 18px 20px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  box-shadow: 0 22px 60px rgba(0, 0, 0, 0.55);
}
.key-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.key-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.key-x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.key-x:hover {
  color: var(--text);
  border-color: var(--line);
}
.key-hint {
  margin: 10px 0 14px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-faint);
}
.key-hint code {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--panel-2);
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-muted);
}
.key-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--text-muted);
}
.key-field input {
  height: 36px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: #080d14;
  color: var(--text);
  font-family: theme('fontFamily.mono');
  font-size: 12.5px;
  outline: none;
}
.key-field input:focus {
  border-color: var(--run-dim);
  box-shadow: var(--ring);
}
.key-msg {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--run);
}
.key-msg.is-bad {
  color: #fca5a5;
}
.key-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
}
.key-spacer {
  flex: 1;
}
.key-btn {
  height: 32px;
  padding: 0 14px;
  border-radius: var(--radius-control);
  font-size: 12.5px;
  cursor: pointer;
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--text-muted);
}
.key-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--line-strong);
}
.key-btn.primary {
  border-color: rgba(34, 197, 94, 0.4);
  background: rgba(34, 197, 94, 0.14);
  color: #86efac;
}
.key-btn.primary:hover:not(:disabled) {
  background: rgba(34, 197, 94, 0.22);
}
.key-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
</style>
