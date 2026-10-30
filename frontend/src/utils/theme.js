/**
 * 主题：唯一来源是 data-theme 属性，值对应 main.css 里 :root[data-theme='x'] 的变量块。
 * 色名（tailwind.config.js）已全部转成 var(--...)，所以切主题只换一批变量值，组件不用动。
 *
 * 作用位置：写在 html 上，全站跟主题，无钉色页 ——
 * 四个页面（写作 / 对话 / 浏览器 / 管理）的色都走同一批变量，
 * 挂在 body 上的浮层（告警条、口令弹窗）直接继承 html 上的主题，不用再单独判。
 */
import { shallowRef } from 'vue'

const KEY = 'w2a_theme'

// swatch 是主题的身份色（导航栏色点用它），故意不跟主题走，否则四个点会长得一样
export const THEMES = [
  { id: 'ink', name: '墨夜', hint: '夜里写，久看不刺眼', swatch: '#1b1f26', edge: '#d9a45b', dark: true },
  { id: 'paper', name: '暖笺', hint: '像纸，白天最舒服', swatch: '#f6efe3', edge: '#a8712a' },
  { id: 'sage', name: '松间', hint: '淡绿，久看不累', swatch: '#e4ebe2', edge: '#4e7a55' },
  { id: 'slate', name: '素白', hint: '亮环境 / 投屏', swatch: '#ffffff', edge: '#94a3b8' },
]

const KNOWN = new Set(THEMES.map((t) => t.id))

// localStorage 不是必然拿得到：禁用站点数据的浏览器里连读都会抛。
// 主题只是偏好，读不到用默认、写不进也别把整页带崩。
function readSaved() {
  try {
    return localStorage.getItem(KEY)
  } catch {
    return null
  }
}

function saveSaved(id) {
  try {
    localStorage.setItem(KEY, id)
  } catch {
    // 存不下就只在这一页生效，下次打开回到默认主题
  }
}

/** 当前主题 id：响应式，导航栏色点和快捷键共用这一个状态，不会各说各话。 */
export const themeId = shallowRef('ink')

export function readTheme() {
  const saved = readSaved()
  // 旧版本存过 'dark'，认作墨夜；不认识的值一律回落默认，避免拿到空的变量块
  if (saved === 'dark') return 'ink'
  return KNOWN.has(saved) ? saved : 'ink'
}

export function isDark(id) {
  return !!THEMES.find((t) => t.id === id)?.dark
}

export function applyTheme(id) {
  const root = document.documentElement
  root.dataset.theme = id
  // 保留 dark class：既有 darkMode: 'class' 的语义不失效（当前没有 dark: 变体在用）
  root.classList.toggle('dark', isDark(id))
  themeId.value = id
  return id
}

export function setTheme(id) {
  saveSaved(id)
  return applyTheme(id)
}

export function cycleTheme() {
  const at = THEMES.findIndex((t) => t.id === themeId.value)
  return setTheme(THEMES[(at + 1) % THEMES.length].id)
}

// index.html 里有一段同义的内联脚本（首帧防闪），改这里记得同步改它
applyTheme(readTheme())
