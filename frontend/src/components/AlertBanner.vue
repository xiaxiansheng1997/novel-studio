<template>
  <Transition name="alert-fade">
    <div v-if="current"
         class="alert-banner"
         :class="'level-' + (current.level || 'warn')"
         role="alert">
      <AppIcon name="alert" :size="16" class="alert-icon" />
      <div class="alert-body">
        <span class="alert-title">{{ title }}</span>
        <span class="alert-msg">{{ current.message }}</span>
      </div>
      <router-link class="alert-action" :to="actionTo" @click="dismiss">去处理</router-link>
      <button class="alert-close" type="button" aria-label="关闭告警" @click="dismiss">
        <AppIcon name="x" :size="13" />
      </button>
    </div>
  </Transition>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from './AppIcon.vue'
import { getApiKey } from '../utils/storage'

// 告警渠道：后端 /admin/alerts/stream（SSE）。EventSource 不能带自定义头，
// 口令通过 ?token= 传；服务端没设口令时 token 为空也能连。
const KIND_LABEL = {
  captcha: '需要人机验证',
  risk: '风控冷却中',
  login: '登录态失效',
  provider: '上游异常',
}

const current = ref(null)
const dismissed = new Set()

let es = null
let retryTimer = 0
let retries = 0

const title = computed(() => {
  if (!current.value) return ''
  const label = KIND_LABEL[current.value.kind] || '运行告警'
  return current.value.provider ? `${current.value.provider} · ${label}` : label
})

// 「去处理」直达控制台的浏览器 tab，并把告警里的 provider 带上
// （以前指向独立的 /live，那条路由现在重定向到同一个位置）
const actionTo = computed(() => ({
  path: '/admin',
  query: { tab: 'live', ...(current.value?.provider ? { provider: current.value.provider } : {}) },
}))

function connect() {
  if (es) {
    es.close()
    es = null
  }
  const qs = new URLSearchParams({ since: '-1' })
  const token = getApiKey()
  if (token) qs.set('token', token)
  es = new EventSource(`/admin/alerts/stream?${qs.toString()}`)

  es.onmessage = ev => {
    retries = 0
    let item = null
    try {
      item = JSON.parse(ev.data)
    } catch (_) {
      return
    }
    if (!item || item.kind === 'hello') return
    if (dismissed.has(item.seq)) return
    current.value = item
  }

  es.onerror = () => {
    // EventSource 自带重连，但口令错/后端挂了会无限敲门：关掉后指数退避（最多 5 次）
    if (es) {
      es.close()
      es = null
    }
    if (retries >= 5) return
    const delay = Math.min(30000, 3000 * 2 ** retries)
    retries += 1
    retryTimer = setTimeout(connect, delay)
  }
}

function dismiss() {
  if (current.value) dismissed.add(current.value.seq)
  current.value = null
}

onMounted(connect)
onUnmounted(() => {
  clearTimeout(retryTimer)
  if (es) es.close()
  es = null
})
</script>

<style scoped>
.alert-banner {
  position: fixed;
  top: 12px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 120;
  display: flex;
  align-items: center;
  gap: 10px;
  width: min(720px, calc(100vw - 100px));
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid rgba(var(--spark-rgb), 0.45);
  background: var(--toast-warn);
  backdrop-filter: blur(8px);
  box-shadow: 0 18px 48px -20px rgba(var(--shade-rgb), 0.9);
  color: var(--spark-fg);
}
.alert-banner.level-error {
  border-color: rgba(var(--alert-rgb), 0.5);
  background: var(--toast-err);
  color: var(--danger-fg-hv);
}
.alert-banner.level-info {
  border-color: var(--line-strong);
  background: var(--toast-info);
  color: var(--text-muted);
}
.alert-icon {
  flex-shrink: 0;
}
.alert-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}
.alert-title {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.01em;
}
.alert-msg {
  font-size: 11.5px;
  line-height: 1.5;
  opacity: 0.9;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.alert-action {
  flex-shrink: 0;
  padding: 4px 10px;
  border-radius: 8px;
  border: 1px solid currentColor;
  color: inherit;
  font-size: 11.5px;
  text-decoration: none;
  opacity: 0.85;
}
.alert-action:hover {
  opacity: 1;
}
.alert-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 6px;
  background: none;
  color: inherit;
  opacity: 0.7;
  cursor: pointer;
}
.alert-close:hover {
  opacity: 1;
}
.alert-fade-enter-active,
.alert-fade-leave-active {
  transition: opacity 0.18s var(--ease), transform 0.18s var(--ease);
}
.alert-fade-enter-from,
.alert-fade-leave-to {
  opacity: 0;
  transform: translate(-50%, -6px);
}
@media (max-width: 720px) {
  .alert-banner {
    width: calc(100vw - 84px);
    left: auto;
    right: 8px;
    transform: none;
  }
  .alert-fade-enter-from,
  .alert-fade-leave-to {
    transform: translateY(-6px);
  }
}
</style>
