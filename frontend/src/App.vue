<template>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <router-view />
</template>

<script setup>
import { onMounted } from 'vue'
import { setTheme } from './utils/storage'

onMounted(() => {
  setTheme('dark')
})
</script>
