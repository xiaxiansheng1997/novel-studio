/**
 * 正文编辑器的排版偏好：字号 / 行距 / 段宽 / 字体 / 护眼 / 专注淡化 / 打字机滚动。
 *
 * 只存 localStorage，不进后端 —— 这是「我在这台机器上看着舒服」，不是作品的属性；
 * 换台电脑打开同一篇，正文一个字不差，只是显示口味回到默认，这样才对。
 *
 * 落地方式：值拼成 --w-* 变量写在排版根节点（StudioView 的 .editor、
 * ReadView 的 .read）上，正文样式靠继承读它们，所以 textarea 的 CSS 里只有
 * var(--w-size) 这种引用，没有第二份需要跟着改的常量。字体栈本体也不在这里：
 * 那里只存档位 id，对应的 --w-<id> 由两个宿主各自从 Tailwind theme 里给全——
 * 哪个宿主漏了四个字体变量，那个页面的正文就掉回浏览器默认字体。
 */
import { computed, ref } from 'vue'

const PREFIX = 'studio:writer:'

// 15 太小没人用、22 一屏剩三行，档位收在这五个里。
// 默认 18 不是随手定的：Windows 的宋体在 ≤17px 走内嵌点阵（实测半覆盖边缘像素
// 恒为 0，也就是没有任何灰度过渡），18px 起才恢复矢量描边。
// 旧默认 17px 正好卡在点阵档里，那是「正文看着奇怪」的主因。
export const FONT_SIZES = [15, 16, 17, 18, 20]
export const LINE_HEIGHTS = [1.7, 1.8, 1.9, 2.0]
// 段宽给的是 CSS 长度，不是数字：满宽就是要跟着窗口走，没有对应的 px
export const MEASURES = [
  { id: 'std', name: '标准', css: '646px', hint: '一行约 38 字，中文最长不累眼的行长' },
  { id: 'wide', name: '宽', css: '720px', hint: '大屏上少几次回车' },
  { id: 'full', name: '满宽', css: '100%', hint: '跟着窗口走，对着旧稿改时方便' },
]
/**
 * 字体档位。id 必须与 .editor 里的 --w-<id> 一一对应：这里只挑名字，
 * 不在 JS 里再写一份字体栈——某个字体在这台机器上装没装，只允许有一处说法。
 */
export const FONT_PRESETS = [
  { id: 'serif', name: '衬线', hint: '默认：西文 Georgia + 中文思源宋体（没装则退到宋体）' },
  { id: 'kai', name: '楷体', hint: '手写感，全程矢量描边，小字号也不碎' },
  { id: 'sans', name: '无衬线', hint: '黑体一类，屏幕上最清楚' },
  { id: 'mono', name: '等宽', hint: '对齐敏感时用，改稿时看结构' },
]

const DEFAULTS = {
  fontSize: 18,
  lineHeight: 1.9,
  measure: 'std',
  font: 'serif',
  tint: false,
  // 专注淡化：非当前段压成底纹；打字机：正在写的那行留在纸面偏上处。
  // 两个都默认关：衬底同步是锦上添花，不该在用户没要求时改变他看到的正文
  focusDim: false,
  typewriter: false,
}

function readNumber(key, allowed, fallback) {
  const raw = localStorage.getItem(PREFIX + key)
  const n = Number(raw)
  return allowed.includes(n) ? n : fallback
}

/** 字体档位：新键优先；没有就从旧版的「衬线/无衬线」布尔迁过来，别让人调过的口味回到默认 */
function readFont() {
  const raw = localStorage.getItem(PREFIX + 'font')
  if (FONT_PRESETS.some((p) => p.id === raw)) return raw
  const legacy = localStorage.getItem(PREFIX + 'serif')
  if (legacy !== null) return legacy === '0' ? 'sans' : 'serif'
  return DEFAULTS.font
}

function load() {
  try {
    const measure = localStorage.getItem(PREFIX + 'measure')
    return {
      fontSize: readNumber('fontSize', FONT_SIZES, DEFAULTS.fontSize),
      lineHeight: readNumber('lineHeight', LINE_HEIGHTS, DEFAULTS.lineHeight),
      measure: MEASURES.some((m) => m.id === measure) ? measure : DEFAULTS.measure,
      font: readFont(),
      tint: localStorage.getItem(PREFIX + 'tint') === '1',
      focusDim: localStorage.getItem(PREFIX + 'focusDim') === '1',
      typewriter: localStorage.getItem(PREFIX + 'typewriter') === '1',
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
  // 存到的值不在档位里（旧版本残留）时，从默认档起步而不是从数组开头
  const base = at < 0 ? FONT_SIZES.indexOf(DEFAULTS.fontSize) : at
  setFontSize(FONT_SIZES[Math.min(FONT_SIZES.length - 1, Math.max(0, base + dir))])
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
export function setFont(id) {
  if (!FONT_PRESETS.some((p) => p.id === id)) return
  writer.value = { ...writer.value, font: id }
  persist('font', id)
}

export function setTint(tint) {
  writer.value = { ...writer.value, tint: !!tint }
  persist('tint', tint ? '1' : '0')
}

export function setFocusDim(on) {
  writer.value = { ...writer.value, focusDim: !!on }
  persist('focusDim', on ? '1' : '0')
}

export function setTypewriter(on) {
  writer.value = { ...writer.value, typewriter: !!on }
  persist('typewriter', on ? '1' : '0')
}

export function resetWriter() {
  writer.value = { ...DEFAULTS }
  for (const [k, v] of Object.entries(DEFAULTS)) persist(k, typeof v === 'boolean' ? (v ? '1' : '0') : v)
}

const measureOf = () => MEASURES.find((m) => m.id === writer.value.measure) || MEASURES[0]
const presetOf = () => FONT_PRESETS.find((p) => p.id === writer.value.font) || FONT_PRESETS[0]

/** 悬停提示与弹层都要用的档位名 */
export const fontName = computed(() => presetOf().name)

/**
 * 绑到 .editor 的 :style 上。护眼用 --shade-rgb 而不是某个固定颜色：
 * 每个主题都给过自己的暗色分量，深色下压一点点黑、暖笺下压一点点褐，
 * 不会出现「切了主题护眼层变成一块灰」的错位。
 *
 * --w-font 只指向 --w-<id>，所以 .editor 里四个字体变量必须全定出来：
 * CSS 变量指向一个没定义的变量时，用到它的那条声明直接失效（不会退到
 * `var(--w-font, …)` 里那个备选值），正文会掉回浏览器默认字体。
 */
export const writerVars = computed(() => ({
  '--w-size': `${writer.value.fontSize}px`,
  '--w-height': String(writer.value.lineHeight),
  '--w-measure': measureOf().css,
  '--w-font': `var(--w-${presetOf().id})`,
  '--w-tint-bg': writer.value.tint ? 'rgba(var(--shade-rgb), 0.05)' : 'transparent',
}))
