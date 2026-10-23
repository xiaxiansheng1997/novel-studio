<template>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <router-view />
  <!-- 全局告警条：验证码/风控/登录失效由后端 SSE 推送，切页面不重建连接 -->
  <AlertBanner />
</template>

<script setup>
import { onMounted } from 'vue'
import AlertBanner from './components/AlertBanner.vue'
import { setTheme } from './utils/storage'

onMounted(() => {
  setTheme('dark')
})
</script>
