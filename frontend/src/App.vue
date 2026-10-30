<template>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <router-view />
  <!-- 全局告警条：验证码/风控/登录失效由后端 SSE 推送，切页面不重建连接 -->
  <AlertBanner />
</template>

<script setup>
import { onMounted, onUnmounted } from 'vue'
import AlertBanner from './components/AlertBanner.vue'
import { cycleTheme } from './utils/theme'

// Ctrl+Alt+T 循环主题：写作页里手一直在键盘上，换底色不该去点鼠标。
// 主题状态在 utils/theme 里，导航栏的色点会跟着亮。
function onKeydown(e) {
  if (e.ctrlKey && e.altKey && !e.shiftKey && e.code === 'KeyT') {
    e.preventDefault()
    cycleTheme()
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
// 主题初值在 main.js 引用 utils/theme 时就落地了，这里只管切换
</script>
