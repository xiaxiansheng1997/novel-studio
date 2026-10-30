import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 既是前端路由又是后端接口前缀的路径（/studio /admin /live）：
// 浏览器整页导航的 Accept 里带 text/html → 交回 Vite 自己的 index.html；
// 其余（fetch 的 */*、EventSource 的 text/event-stream）才转给后端。
// 少了这一层，dev 下直接刷新这三页会拿到后端 dist/index.html，
// 它引用的 /assets/*.js 不回代理、在 dev 端口上 404，结果就是一片空白。
const isHtmlNav = (req) => ((req.headers.accept || '').includes('text/html') ? '/index.html' : undefined)

export default defineConfig({
  plugins: [vue()],
  server: {
    // 不用 Vite 默认的 5173：同一台机器上其它项目的 dev server 会占住它，
    // Vite 随默认值往 5174/5175 悄悄退，浏览器里看到的其实是别人的应用。
    // 5273 在这个项目里定死，strictPort 让“被占”当场报错而不是静默跳号。
    port: 5273,
    strictPort: true,
    proxy: {
      '/v1': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/admin': { target: 'http://127.0.0.1:8000', changeOrigin: true, bypass: isHtmlNav },
      '/live': { target: 'http://127.0.0.1:8000', changeOrigin: true, bypass: isHtmlNav },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      // /studio 既是前端路由（/studio/:id 是页面）又是后端接口前缀：
      // 少了这条，dev 下所有写作台接口都拿到 index.html，前端 JSON 解析失败、
      // 书架永远是空的（点卡片也打不开，界面上还看不到报错）。
      '/studio': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        bypass: isHtmlNav,
      },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
