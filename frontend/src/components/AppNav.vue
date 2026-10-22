<template>
  <nav class="rail" aria-label="主导航">
    <router-link to="/chat" class="rail-logo" aria-label="Web2API 控制台首页">
      <span class="logo-mark">W2</span>
    </router-link>

    <div class="rail-items">
      <router-link
        v-for="item in items"
        :key="item.to"
        :to="item.to"
        class="rail-item"
        :class="{ 'is-active': isActive(item) }"
        :aria-current="isActive(item) ? 'page' : undefined"
      >
        <AppIcon :name="item.icon" :size="20" />
        <span class="rail-label">{{ item.label }}</span>
      </router-link>
    </div>

    <div class="rail-foot">
      <button
        type="button"
        class="rail-key"
        :class="{ 'is-set': hasKey }"
        :title="hasKey ? '已配置访问口令（点击修改）' : '设置访问口令'"
        @click="keyOpen = true"
      >
        <AppIcon name="key" :size="18" />
        <span v-if="hasKey" class="key-dot"></span>
      </button>
      <span class="rail-version">v0.1</span>
    </div>
  </nav>

  <ApiKeyDialog v-model:open="keyOpen" />
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AppIcon from './AppIcon.vue'
import ApiKeyDialog from './ApiKeyDialog.vue'
import { getApiKey } from '../utils/storage'

const route = useRoute()
const keyOpen = ref(false)
const hasKey = ref(Boolean(getApiKey()))

const items = [
  { to: '/chat', label: '对话', icon: 'chat' },
  { to: '/live', label: '浏览器', icon: 'monitor' },
  { to: '/admin', label: '管理', icon: 'sliders' },
]

function isActive(item) {
  if (item.to === '/chat') return route.path === '/' || route.path.startsWith('/chat')
  return route.path.startsWith(item.to)
}

// 服务端设了口令而本地还没填：所有受保护请求都会 401，不如进来就提醒一次。
// AppNav 在每个视图里都会重新挂载，用 sessionStorage 标记保证一个会话只自动弹一次。
const HINT_KEY = 'w2a_key_hinted'

onMounted(async () => {
  if (hasKey.value) return
  try {
    if (sessionStorage.getItem(HINT_KEY) === '1') return
  } catch (_) {
    /* 隐私模式下 sessionStorage 不可用，宁可多弹一次 */
  }
  try {
    const r = await fetch('/health')
    const j = await r.json()
    if (j && j.auth_required) {
      keyOpen.value = true
      try { sessionStorage.setItem(HINT_KEY, '1') } catch (_) { /* 忽略 */ }
    }
  } catch (_) {
    /* 后端没起来/拿不到健康检查就不弹 */
  }
})
</script>

<style scoped>
.rail {
  width: 76px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 18px;
  padding: 16px 0 14px;
  background: linear-gradient(180deg, #0a0f17 0%, #070a10 100%);
  border-right: 1px solid var(--line-soft);
  z-index: 20;
}
.rail-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 42px;
  height: 42px;
  border-radius: 12px;
  text-decoration: none;
  background: linear-gradient(135deg, rgba(34, 197, 94, 0.22), rgba(56, 189, 248, 0.14));
  border: 1px solid rgba(34, 197, 94, 0.35);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
}
.logo-mark {
  font-family: theme('fontFamily.mono');
  font-size: 13px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: #86efac;
}
.rail-items {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 0 10px;
}
.rail-item {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 9px 0 7px;
  border-radius: 10px;
  color: var(--text-faint);
  text-decoration: none;
  transition: color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.rail-item:hover {
  color: var(--text);
  background: rgba(34, 197, 94, 0.06);
}
.rail-item.is-active {
  color: var(--run);
  background: rgba(34, 197, 94, 0.1);
}
.rail-item.is-active::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 12px;
  bottom: 12px;
  width: 2px;
  border-radius: 2px;
  background: var(--run);
}
.rail-label {
  font-size: 10.5px;
  letter-spacing: 0.02em;
}
.rail-foot {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  margin-top: auto;
  color: var(--text-faint);
}
.rail-key {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  transition: color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.rail-key:hover {
  color: var(--text);
  border-color: var(--line);
  background: rgba(34, 197, 94, 0.06);
}
.rail-key.is-set {
  color: var(--run);
}
.key-dot {
  position: absolute;
  top: 5px;
  right: 5px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--run);
  box-shadow: 0 0 0 2px #080d14;
}
.rail-version {
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  opacity: 0.75;
}

@media (max-width: 720px) {
  .rail {
    width: 60px;
    gap: 12px;
    padding-top: 12px;
  }
  .rail-logo {
    width: 36px;
    height: 36px;
    border-radius: 10px;
  }
  .rail-items {
    padding: 0 7px;
  }
  .rail-label {
    display: none;
  }
  .rail-item {
    padding: 10px 0;
  }
  .rail-version {
    display: none;
  }
}
</style>
