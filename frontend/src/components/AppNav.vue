<template>
  <nav class="rail" aria-label="主导航">
    <router-link to="/studio" class="rail-logo" aria-label="Novel Studio 写作台">
      <span class="logo-mark">墨</span>
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
      <!-- 主题全站生效：写作台与控制台读同一批变量。
           themeId 是 <script setup> 里 import 进来的 ref，模板已经替它解过包，写 .value 反而拿不到值 -->
      <div class="rail-themes" role="group" aria-label="主题">
        <button
          v-for="th in THEMES"
          :key="th.id"
          type="button"
          class="rail-swatch"
          :class="{ 'is-on': th.id === themeId }"
          :style="{ background: th.swatch, borderColor: th.edge }"
          :title="`主题：${th.name} · ${th.hint}${th.id === themeId ? '（当前）' : ''}`"
          :aria-pressed="th.id === themeId"
          @click="setTheme(th.id)"
        >
          <span class="sr-only">{{ th.name }}</span>
        </button>
      </div>
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
      <span class="rail-version">v0.2</span>
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
import { THEMES, setTheme, themeId } from '../utils/theme'

const route = useRoute()
const keyOpen = ref(false)
const hasKey = ref(Boolean(getApiKey()))

// 只有两项：写作是工作台，其余（浏览器/管理/日志/任务…）都收进控制台
const items = [
  { to: '/studio', label: '写作', icon: 'book' },
  { to: '/admin', label: '控制台', icon: 'sliders' },
]

function isActive(item) {
  if (item.to === '/studio') return route.path === '/' || route.path.startsWith('/studio')
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
/* 54px：写作时把横向空间还给正文，悬停才出文字 */
.rail {
  width: 54px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 14px 0 12px;
  background: var(--panel);
  border-right: 1px solid var(--line-soft);
  z-index: 20;
}
.rail-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  text-decoration: none;
  background: rgba(var(--run-rgb), 0.1);
  border: 1px solid rgba(var(--run-rgb), 0.28);
  transition: background-color 0.16s var(--ease), border-color 0.16s var(--ease);
}
.rail-logo:hover {
  background: rgba(var(--run-rgb), 0.16);
  border-color: rgba(var(--run-rgb), 0.45);
}
.logo-mark {
  font-family: theme('fontFamily.serif');
  font-size: 16px;
  font-weight: 600;
  line-height: 1;
  color: var(--run-fg);
}
.rail-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  padding: 0 7px;
}
.rail-item {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 8px 0 6px;
  border-radius: 8px;
  color: var(--text-faint);
  text-decoration: none;
  cursor: pointer;
  transition: color 0.16s var(--ease), background-color 0.16s var(--ease);
}
.rail-item:hover {
  color: var(--text);
  background: rgba(var(--run-rgb), 0.06);
}
.rail-item.is-active {
  color: var(--run-fg);
  background: rgba(var(--run-rgb), 0.1);
}
.rail-item.is-active::before {
  content: '';
  position: absolute;
  left: -7px;
  top: 10px;
  bottom: 10px;
  width: 2px;
  border-radius: 2px;
  background: var(--run);
}
.rail-label {
  font-size: 10px;
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
.rail-themes {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--line-soft);
  border-radius: 9px;
}
.rail-swatch {
  width: 13px;
  height: 13px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  cursor: pointer;
  opacity: 0.5;
  transition: opacity 0.16s var(--ease), box-shadow 0.16s var(--ease);
}
.rail-swatch:hover {
  opacity: 0.85;
}
.rail-swatch.is-on {
  opacity: 1;
  /* 当前主题靠外圈实线表示，不靠颜色：四个色点在暗栏里差别不够大 */
  box-shadow: 0 0 0 2px var(--panel), 0 0 0 3px var(--run);
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
  background: rgba(var(--run-rgb), 0.06);
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
  box-shadow: 0 0 0 2px var(--panel);
}
.rail-version {
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  opacity: 0.75;
}

@media (max-width: 720px) {
  .rail {
    width: 46px;
    gap: 10px;
    padding-top: 10px;
  }
  .rail-logo {
    width: 30px;
    height: 30px;
    border-radius: 8px;
  }
  .logo-mark {
    font-size: 14px;
  }
  .rail-items {
    padding: 0 5px;
  }
  .rail-label {
    display: none;
  }
  .rail-item {
    padding: 9px 0;
  }
  .rail-version {
    display: none;
  }
}
</style>
