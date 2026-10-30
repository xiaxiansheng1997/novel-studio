import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/studio' },
  // 写作台是工作台，控制台是它的后勤（状态 / 浏览器 / 日志 / 任务 / 数据 / 配置）
  { path: '/studio', name: 'studio', component: () => import('./views/StudioView.vue') },
  { path: '/studio/:id', name: 'studio-work', component: () => import('./views/StudioView.vue') },
  // 对话已并进写作台的「问一问」：老链接带过去，具体会话用 ?conv= 带
  { path: '/chat', redirect: () => ({ path: '/studio', query: { panel: 'ask' } }) },
  {
    path: '/chat/:id',
    redirect: (to) => ({ path: '/studio', query: { panel: 'ask', conv: String(to.params.id || '') } }),
  },
  { path: '/admin', name: 'console', component: () => import('./views/ConsoleView.vue') },
  // 老链接：内置浏览器已并进控制台的「浏览器」tab，query（provider / url）原样带过去
  {
    path: '/live',
    redirect: (to) => ({ path: '/admin', query: { ...to.query, tab: 'live' } }),
  },
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