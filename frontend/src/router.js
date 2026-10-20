import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', name: 'chat', component: () => import('./views/ChatView.vue') },
  { path: '/chat/:id', name: 'chat-conversation', component: () => import('./views/ChatView.vue') },
  { path: '/admin', name: 'admin', component: () => import('./views/AdminView.vue') },
  { path: '/live', name: 'live', component: () => import('./views/LiveView.vue') },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
