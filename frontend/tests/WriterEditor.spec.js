/**
 * 块 1「编辑器换底」的三条命脉：
 * 1. paras.js 切分一个字符不丢 —— 衬底敢和 textarea 叠在一起全押在这上面；
 * 2. 输入法合成中途绝不 emit —— :value 被 Vue 回写会掐断正在拼的拼音；
 * 3. expose 伪装成 textarea —— StudioView 的查找/定位/加工四处消费点零改动。
 */
import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import { nextTick } from 'vue'

import WriterEditor from '../src/components/WriterEditor.vue'
import { activePartAt, splitWithOffsets } from '../src/utils/paras'

// ==================== paras.js ====================

it('切分一个字符不丢：按序拼回 === 原文，偏移首尾相接', () => {
  for (const s of ['', 'abc', 'a\n\nb', 'a\nb', 'a\n \nb', '\n\nx', 'x\n\n', 'a\n\n\n\nb']) {
    const parts = splitWithOffsets(s)
    expect(parts.map((p) => p.text).join('')).toBe(s)
    let pos = 0
    for (const p of parts) {
      expect(p.start).toBe(pos)
      pos += p.text.length
      expect(p.end).toBe(pos)
    }
  }
})

it('空行整体当一个分隔符，连续换行不切出空正文段', () => {
  const parts = splitWithOffsets('a\n\n\nb')
  expect(parts.map((p) => [p.text, p.sep])).toEqual([
    ['a', false], ['\n\n\n', true], ['b', false],
  ])
})

it('光标在段尾/分隔符里时淡化不跳段：退回左边最近的正文段', () => {
  const parts = splitWithOffsets('aa\n\nbb')
  expect(activePartAt(parts, 0)).toBe(0)   // 段内
  expect(activePartAt(parts, 2)).toBe(0)   // 第一段尾（也是分隔符起点）：还没按回车，不该跳
  expect(activePartAt(parts, 3)).toBe(0)   // 分隔符中间
  expect(activePartAt(parts, 4)).toBe(2)   // 站进第二段
})

it('没有正文可站就是 -1', () => {
  expect(activePartAt(splitWithOffsets(''), 0)).toBe(-1)
  expect(activePartAt(splitWithOffsets('\n\n'), 0)).toBe(-1)
})

// ==================== WriterEditor ====================

function mountRaw(props = {}) {
  return mount(WriterEditor, { props: { modelValue: 'aa\n\nbb', ...props } })
}

// onMounted 里的 syncActive 要等一次微任务才落到 DOM（真实浏览器不存在这个窗口）
async function mountEditor(props = {}) {
  const w = mountRaw(props)
  await nextTick()
  return w
}

it('两个开关都关时没有衬底：不该在用户没要求时改变他看到的正文', async () => {
  const w = await mountEditor()
  expect(w.find('.ue-under').exists()).toBe(false)
  expect(w.find('.ue').classes()).not.toContain('ue-over')
})

it('focusDim 开：非当前段压 dim，光标移过去 dim 跟着移', async () => {
  const w = await mountEditor({ focusDim: true })
  let ps = w.findAll('.ue-p')
  expect(ps).toHaveLength(2)
  // happy-dom 里未聚焦 textarea 的 cursor 默认停在文末，所以初始站在最后一段
  expect(ps[0].classes()).toContain('dim')
  expect(ps[1].classes()).not.toContain('dim')
  w.vm.setSelectionRange(1, 1)                   // 移到第一段：expose 代理内部会顺带 syncActive
  await nextTick()
  ps = w.findAll('.ue-p')
  expect(ps[0].classes()).not.toContain('dim')
  expect(ps[1].classes()).toContain('dim')
})

it('合成中途不 emit；compositionend 补发一次', async () => {
  const w = await mountEditor()
  const ta = w.find('textarea')
  await ta.trigger('compositionstart')
  ta.element.value = 'aa\n\nbbpin'
  await ta.trigger('input')
  expect(w.emitted('update:modelValue')).toBeUndefined()
  expect(w.emitted('input')).toBeUndefined()
  await ta.trigger('compositionend')
  expect(w.emitted('update:modelValue')[0][0]).toBe('aa\n\nbbpin')
  expect(w.emitted('input')).toHaveLength(1)
})

it('非合成的输入正常发 update:modelValue + input', async () => {
  const w = await mountEditor()
  const ta = w.find('textarea')
  ta.element.value = 'aa\n\nbbc'
  await ta.trigger('input')
  expect(w.emitted('update:modelValue')[0][0]).toBe('aa\n\nbbc')
})

it('打字机开：活跃段里放零宽标记；合成中不放（衬底此刻对不上）', async () => {
  const w = await mountEditor({ typewriter: true })
  expect(w.find('.ue-mark').exists()).toBe(true)
  await w.find('textarea').trigger('compositionstart')
  await nextTick()
  expect(w.find('.ue-mark').exists()).toBe(false)
})

it('expose 就是 textarea 的替身：消费面不用认识组件', async () => {
  const w = await mountEditor()
  const vm = w.vm
  expect(vm.el.tagName).toBe('TEXTAREA')
  vm.setSelectionRange(1, 3)
  expect(vm.selectionStart).toBe(1)
  expect(vm.selectionEnd).toBe(3)
  vm.scrollTop = 42                     // 定位高亮靠它；setter 会同步衬底
  expect(vm.el.scrollTop).toBe(42)
  expect(vm.scrollTop).toBe(42)
  expect(typeof vm.scrollHeight).toBe('number')
})

it('activeIndex 跟着选区走：段导航浮层靠它标出当前段', async () => {
  const w = await mountEditor()
  expect(w.vm.activeIndex).toBe(2)      // happy-dom 里 cursor 默认停文末：站在最后一段
  w.vm.setSelectionRange(1, 1)
  await nextTick()
  expect(w.vm.activeIndex).toBe(0)
})

it('scrollToOffset 量完就清空测量层，不会把正文留两份', async () => {
  const w = await mountEditor()
  w.vm.scrollToOffset(4)
  const measure = w.find('.ue-measure').element
  expect(measure.textContent).toBe('')  // 量完即收：衬底关着时它是唯一碰正文的地方
  // happy-dom 没有布局，offsetTop 恒为 0：这里只验「不报错也不滚出负数」
  expect(w.find('textarea').element.scrollTop).toBeGreaterThanOrEqual(0)
})
