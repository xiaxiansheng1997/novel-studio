import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', name: 'chat', component: () => import('./views/ChatView.vue') },
  { path: '/chat/:id', name: 'chat-conversation', component: () => import('./views/ChatView.vue') },
  { path: '/admin', name: 'admin', component: () => import('./views/AdminView.vue') },
  { path: '/live', name: 'live', component: () => import('./views/LiveView.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 重新 build 后旧页面引用的动态 chunk 已换成新 hash，点菜单会报
// “Failed to fetch dynamically imported module”——表现就是“菜单点不动”。
// 这里兑底整页刷新一次（10s 内只刷一次，防死循环），把用户带到新构建。
let lastChunkReload = 0
router.onError((err, to) => {
  const msg = String((err && err.message) || err)
  if (!/dynamically imported module|Importing a module script failed|Loading chunk/i.test(msg)) return
  const now = Date.now()
  if (now - lastChunkReload < 10000) return
  lastChunkReload = now
  location.assign(to && to.fullPath ? to.fullPath : location.href)
})

export default router