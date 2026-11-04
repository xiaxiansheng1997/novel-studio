<template>
  <div class="ue" :class="{ 'ue-over': underOn }">
    <!--
      衬底：textarea 的字变透明，看到的就是这一层（光标由 caret-color 画出，
      选区高亮由 ::selection 画出，都还在 textarea 自己身上）。
      对齐的关键在 splitWithOffsets：正文和分隔符一个字符不丢、原样渲染，
      pre-wrap 的换行行为与 textarea 逐字同构——间距靠 CSS 补是追不上的。
      width 用 JS 里的 clientWidth：滚动条占掉的宽度 textarea 包在 clientWidth 里，
      CSS 变量给不出这个数，写死 100% 就会比折行宽度多出一条，中文断行错位。
    -->
    <div
      v-if="underOn"
      ref="underEl"
      class="ue-under"
      :style="{ width: clientW ? clientW + 'px' : '', transform: `translateY(${-scrollY}px)` }"
      aria-hidden="true"
    >
      <template v-for="(p, i) in parts" :key="i">
        <span v-if="p.sep">{{ p.text }}</span>
        <span v-else class="ue-p" :class="{ on: i === activeIdx, dim: focusDim && i !== activeIdx }">
          <template v-if="i === markIdx">
            {{ beforeCaret }}<span ref="markEl" class="ue-mark"></span>{{ afterCaret }}
          </template>
          <template v-else>{{ p.text }}</template>
        </span>
      </template>
    </div>

    <textarea
      ref="taEl"
      class="ue-ta"
      :value="modelValue"
      :placeholder="placeholder"
      spellcheck="false"
      @input="onInput"
      @scroll="onScroll"
      @compositionstart="composing = true"
      @compositionend="onCompositionEnd"
    />

    <!--
      离屏测量层：回答「正文里第 pos 个字符落在第几行」——段导航要跳到
      没在屏幕上的位置，衬底又未必开着，只能拿一份隐藏的同构布局量。
      与衬底共用同一组度量（CSS 选择器组），宽度同 clientW；
      内容命令式塞入，不进 Vue 渲染。真实浏览器里同步读 offsetTop 会强制
      layout，量一次几毫秒；happy-dom 里永远是 0，测行为不测像素。
    -->
    <div
      ref="measureEl"
      class="ue-measure"
      :style="{ width: clientW ? clientW + 'px' : '' }"
      aria-hidden="true"
    ></div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { activePartAt, splitWithOffsets } from '../utils/paras'

const props = defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '' },
  // 专注淡化：非当前段压成底纹
  focusDim: { type: Boolean, default: false },
  // 打字机：正在写的那行停在纸面偏上处，往下写时文字向上走
  typewriter: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue', 'input'])

const taEl = ref(null)
const underEl = ref(null)
const markEl = ref(null)
const measureEl = ref(null)
const composing = ref(false)
const scrollY = ref(0)
const clientW = ref(0)
const activeIdx = ref(-1)

const parts = computed(() => splitWithOffsets(props.modelValue))
const underOn = computed(() => !!(props.focusDim || props.typewriter))

// 打字机要量「光标在这一行的哪」：在活跃段的 caret 位置放一个零宽标记，
// 读它的 offsetTop。输入法合成中不放——preedit 文本只存在于 textarea 里，
// 衬底此刻与内容对不上，量出来的行位是错的。
const markIdx = computed(() => {
  if (!props.typewriter || !underOn.value || composing.value) return -1
  const p = parts.value[activeIdx.value]
  return p && !p.sep ? activeIdx.value : -1
})

function caretInPart(i) {
  const p = parts.value[i]
  const pos = taEl.value?.selectionStart ?? p.start
  return Math.max(0, Math.min(pos - p.start, p.text.length))
}
const beforeCaret = computed(() =>
  (markIdx.value < 0 ? '' : parts.value[markIdx.value].text.slice(0, caretInPart(markIdx.value))))
const afterCaret = computed(() =>
  (markIdx.value < 0 ? '' : parts.value[markIdx.value].text.slice(caretInPart(markIdx.value))))

function syncActive() {
  const ta = taEl.value
  if (!ta) { activeIdx.value = -1; return }
  activeIdx.value = activePartAt(parts.value, ta.selectionStart ?? 0)
}

function onScroll() {
  const ta = taEl.value
  if (ta) scrollY.value = ta.scrollTop
}

/**
 * 打字机跟的是「在写的那一行」，不是光标被点到哪就跟到哪：
 * 只在输入后调用。鼠标点去前文翻看时滚动归人自己，别抢。
 */
function followLine() {
  if (!props.typewriter || composing.value) return
  const ta = taEl.value
  const mk = markEl.value
  if (!ta || !mk) return
  // mark 的 offsetTop 相对衬底（position 定位祖先），不含 translateY ——
  // 与 textarea 的滚动内容坐标同一系：两边 padding-top 都是 8px
  const lineH = parseFloat(getComputedStyle(ta).lineHeight) || 28
  const want = Math.max(0, mk.offsetTop - ta.clientHeight * 0.42)
  // 容差半行：每敲一个字都精确归位反而像在抖，差得不多就不动
  if (Math.abs(ta.scrollTop - want) > lineH * 0.55) {
    ta.scrollTop = want
    scrollY.value = want // 程序赋值也会触发 scroll，但这里直接同步，省一帧衬底漂移
  }
}

/**
 * 把正文偏移 pos 处滚到视口上三分之一：段导航的跳转落点。
 * 在测量层里重现「pos 之前的字」的排布，末尾补一个零宽点读它的
 * offsetTop——两侧 padding-top 都是 8px，与 ta 的滚动内容同坐标系。
 * 落点取三分之一而不是贴顶：跳过去还要看见上一段的尾巴才知道自己在哪。
 */
function scrollToOffset(pos) {
  const ta = taEl.value
  const el = measureEl.value
  if (!ta || !el) return
  const text = props.modelValue || ''
  el.textContent = text.slice(0, Math.max(0, Math.min(pos, text.length)))
  const dot = document.createElement('span')
  // 命令式创建的元素拿不到 scoped 属性，零宽样式只能直接写
  dot.style.cssText = 'display:inline-block;width:0;height:0'
  el.appendChild(dot)
  const y = dot.offsetTop
  el.textContent = ''
  ta.scrollTop = Math.max(0, y - ta.clientHeight * 0.34)
  // 程序赋值不保证走 scroll 事件（已贴顶时为 0），衬底直接跟到位
  scrollY.value = ta.scrollTop
}

function onInput() {
  const ta = taEl.value
  if (!ta) return
  // 合成中途不 emit：:value 被 Vue 回写会掐断输入法正在拼的那串拼音
  if (composing.value) return
  emit('update:modelValue', ta.value)
  emit('input')
  nextTick(() => { syncActive(); followLine() })
}

function onCompositionEnd() {
  composing.value = false
  onInput()
}

// 光标移动（含方向键/点击）：淡化段要跟过去；打字机不在这里跟随（见 followLine）
function onSelChange() {
  if (document.activeElement === taEl.value) syncActive()
}

let ro = null
onMounted(() => {
  const ta = taEl.value
  if (ta) {
    clientW.value = ta.clientWidth
    // textarea 永远预留滚动条（overflow-y: scroll），clientWidth 只随元素尺寸变
    if (window.ResizeObserver) {
      ro = new ResizeObserver(() => { clientW.value = ta.clientWidth })
      ro.observe(ta)
    }
  }
  document.addEventListener('selectionchange', onSelChange)
  syncActive()
})
onBeforeUnmount(() => {
  document.removeEventListener('selectionchange', onSelChange)
  ro?.disconnect()
})

// 正文来自外部（采用生成、替换回灌）：段表变了要重新对表，衬底滚动跟一次
watch(() => props.modelValue, () => {
  nextTick(() => { syncActive(); onScroll() })
})

// 衬底是 v-if 挂的：刚打开开关时它的 scrollTop 还停在 0，先跟一次正文
watch(underOn, (on) => { if (on) nextTick(onScroll) })

/*
 * 对外伪装成 textarea：StudioView 的查找/定位/加工选区四处都直接使唤它，
 * 一行都不用改。selectionStart 之类别名成 getter，读到的永远是内部真值。
 */
defineExpose({
  get el() { return taEl.value },
  get selectionStart() { return taEl.value?.selectionStart ?? 0 },
  get selectionEnd() { return taEl.value?.selectionEnd ?? 0 },
  get scrollHeight() { return taEl.value?.scrollHeight ?? 0 },
  get clientHeight() { return taEl.value?.clientHeight ?? 0 },
  get scrollTop() { return taEl.value?.scrollTop ?? 0 },
  // 当前段下标：段导航浮层靠它标出「你在哪一段」
  get activeIndex() { return activeIdx.value },
  set scrollTop(v) {
    if (taEl.value) { taEl.value.scrollTop = v; scrollY.value = v }
  },
  setSelectionRange(a, b) {
    taEl.value?.setSelectionRange(a, b)
    syncActive()
  },
  scrollToOffset,
  focus(opts) { taEl.value?.focus(opts) },
})
</script>

<style scoped>
.ue {
  position: relative;
  width: min(var(--w-measure, 646px), 100%);
  height: 100%;
}
/* textarea 与两层纸共用一整组度量：任何一个属性走偏都会变成「字糊成重影」；
   测量层不叫 ue-under：测试与调试都在找「衬底开没开」，两层得分得开 */
.ue-ta,
.ue-under,
.ue-measure {
  font-family: var(--w-font, var(--w-serif));
  font-size: var(--w-size, 18px);
  line-height: var(--w-height, 1.9);
  letter-spacing: 0.01em;
  white-space: pre-wrap;
  word-break: normal;
  padding: 8px 0 30px;
  box-sizing: border-box;
}
.ue-ta {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  resize: none;
  border: none;
  background: transparent;
  color: var(--text);
  outline: none;
  /* 永远预留滚动条：衬底的宽度拿的是 clientWidth，忽现忽隐会让折行宽度跳变 */
  overflow-y: scroll;
}
.ue-under,
.ue-measure {
  position: absolute;
  top: 0;
  left: 0;
  overflow: hidden;
  color: var(--text);
  pointer-events: none;
}
.ue-under {
  will-change: transform;
}
/* 衬底开着：字在这层，textarea 只留光标与选区 */
.ue-over .ue-ta {
  color: transparent;
  caret-color: var(--text);
}
.ue-over .ue-ta::selection {
  /* 字透明后浏览器默认的半透明蓝压在最上层很扎眼，改成主题的底色高亮 */
  background: rgba(var(--run-rgb), 0.22);
}
.ue-ta::placeholder {
  color: var(--text-faint);
  font-family: theme('fontFamily.sans');
  font-size: 13.5px;
  line-height: 1.7;
}
.ue-p {
  transition: opacity 0.18s ease;
}
.ue-p.dim {
  opacity: 0.4;
}
.ue-mark {
  display: inline-block;
  width: 0;
  height: 0;
}
.ue-measure {
  /* 量完即清空：它常驻 DOM，但不能挡住或抢走任何交互 */
  visibility: hidden;
  z-index: -1;
}
@media (prefers-reduced-motion: reduce) {
  .ue-p { transition: none; }
}
</style>
