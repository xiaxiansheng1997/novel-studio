/**
 * 正文编辑器的排版偏好：字号 / 行距 / 段宽 / 字体 / 护眼。
 *
 * 只存 localStorage，不进后端 —— 这是「我在这台机器上看着舒服」，不是作品的属性；
 * 换台电脑打开同一篇，正文一个字不差，只是显示口味回到默认，这样才对。
 *
 * 落地方式：值拼成 --w-* 变量写在编辑器根节点（StudioView 的 .editor）上，
 * 正文样式靠继承读它们，所以 textarea 的 CSS 里只有 var(--w-size) 这种引用，
 * 没有第二份需要跟着改的常量。
 */
import { computed, ref } from 'vue'

const PREFIX = 'studio:writer:'

// 15 太小没人用、22 一屏剩三行，档位收在这五个里；默认 17 与改动前逐像素一致
export const FONT_SIZES = [15, 16, 17, 18, 20]
export const LINE_HEIGHTS = [1.7, 1.8, 1.9, 2.0]
// 段宽给的是 CSS 长度，不是数字：满宽就是要跟着窗口走，没有对应的 px
export const MEASURES = [
  { id: 'std', name: '标准', css: '646px', hint: '一行约 38 字，中文最长不累眼的行长' },
  { id: 'wide', name: '宽', css: '720px', hint: '大屏上少几次回车' },
  { id: 'full', name: '满宽', css: '100%', hint: '跟着窗口走，对着旧稿改时方便' },
]

const DEFAULTS = {
  fontSize: 17,
  lineHeight: 1.9,
  measure: 'std',
  serif: true,
  tint: false,
}

function readNumber(key, allowed, fallback) {
  const raw = localStorage.getItem(PREFIX + key)
  const n = Number(raw)
  return allowed.includes(n) ? n : fallback
}

function load() {
  try {
    const measure = localStorage.getItem(PREFIX + 'measure')
    return {
      fontSize: readNumber('fontSize', FONT_SIZES, DEFAULTS.fontSize),
      lineHeight: readNumber('lineHeight', LINE_HEIGHTS, DEFAULTS.lineHeight),
      measure: MEASURES.some((m) => m.id === measure) ? measure : DEFAULTS.measure,
      serif: localStorage.getItem(PREFIX + 'serif') !== '0',
      tint: localStorage.getItem(PREFIX + 'tint') === '1',
    }
  } catch {
    // 隐私模式下 localStorage 会抛：排版退回默认值就行，写作不能因此中断
    return { ...DEFAULTS }
  }
}

export const writer = ref(load())

function persist(key, value) {
  try {
    localStorage.setItem(PREFIX + key, String(value))
  } catch {
    /* 存不下就只在本次会话有效，不值得为这个弹提示 */
  }
}

export function setFontSize(n) {
  if (!FONT_SIZES.includes(n)) return
  writer.value = { ...writer.value, fontSize: n }
  persist('fontSize', n)
}

/** Ctrl+= / Ctrl+- 走这里：在当前档位上上下下一格，到底就停住 */
function bumpFontSize(dir) {
  const at = FONT_SIZES.indexOf(writer.value.fontSize)
  const next = FONT_SIZES[Math.min(FONT_SIZES.length - 1, Math.max(0, (at < 0 ? 2 : at) + dir))]
  setFontSize(next)
}

export const bumpFontUp = () => bumpFontSize(1)
export const bumpFontDown = () => bumpFontSize(-1)

export function setLineHeight(n) {
  if (!LINE_HEIGHTS.includes(n)) return
  writer.value = { ...writer.value, lineHeight: n }
  persist('lineHeight', n)
}

export function setMeasure(id) {
  if (!MEASURES.some((m) => m.id === id)) return
  writer.value = { ...writer.value, measure: id }
  persist('measure', id)
}

// 弹层里是「选一个」而不是「翻一下」，所以给确定值的 setter，不提供 toggle：
// 两个 chip 共用 toggle 的话，点了已选中的那个会把状态倒过去。
export function setSerif(serif) {
  writer.value = { ...writer.value, serif: !!serif }
  persist('serif', serif ? '1' : '0')
}

export function setTint(tint) {
  writer.value = { ...writer.value, tint: !!tint }
  persist('tint', tint ? '1' : '0')
}

export function resetWriter() {
  writer.value = { ...DEFAULTS }
  for (const [k, v] of Object.entries(DEFAULTS)) persist(k, typeof v === 'boolean' ? (v ? '1' : '0') : v)
}

const measureOf = () => MEASURES.find((m) => m.id === writer.value.measure) || MEASURES[0]

/**
 * 绑到 .editor 的 :style 上。护眼用 --shade-rgb 而不是某个固定颜色：
 * 每个主题都给过自己的暗色分量，深色下压一点点黑、暖笺下压一点点褐，
 * 不会出现「切了主题护眼层变成一块灰」的错位。
 */
export const writerVars = computed(() => ({
  '--w-size': `${writer.value.fontSize}px`,
  '--w-height': String(writer.value.lineHeight),
  '--w-measure': measureOf().css,
  '--w-font': writer.value.serif ? 'var(--w-serif)' : 'var(--w-sans)',
  '--w-tint-bg': writer.value.tint ? 'rgba(var(--shade-rgb), 0.05)' : 'transparent',
}))
