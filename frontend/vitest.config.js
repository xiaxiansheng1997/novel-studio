import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 前端单测独立配置：不并进 vite.config.js，dev/build 的行为一点不碰。
// CSS 默认不编译（scoped 样式里的 theme() 不会执行），组件行为测够了冒烟交给 Playwright。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'happy-dom',
    include: ['tests/**/*.spec.js'],
  },
})
