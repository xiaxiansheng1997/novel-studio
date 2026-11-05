/**
 * StudioTree：结构菜单里能做什么。
 * 盯三件在「按卷导入」之后才会显出来的事：
 *   1) 卷也要有上移/下移——批量导进来几卷，顺序常要挪，换的是整棵子树；
 *   2) 首/末的置灰按「同一父节点 + 同一类（卷 或 章）」分组算，不能跨卷比；
 *   3) 章要能「移出到顶层」——一本书只有卷时，拖拽没有空白处可落。
 * 菜单 teleport 到 body，断言直接查 document.body。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it } from 'vitest'

// eslint-disable-next-line import/first
import StudioTree from '../src/components/StudioTree.vue'

const node = (over) => ({
  parent_id: null, depth: 0, ordinal: null, kind: 'volume', status: 'draft',
  word_count: 0, ...over,
})

const TREE = [
  node({ id: 'v1', path: '0001', title: '第一卷' }),
  node({ id: 'c1', parent_id: 'v1', path: '0001.0001', depth: 1, ordinal: 1,
    kind: 'chapter', title: '第一章', word_count: 120 }),
  node({ id: 'c2', parent_id: 'v1', path: '0001.0002', depth: 1, ordinal: 2,
    kind: 'chapter', title: '第二章', word_count: 130 }),
  node({ id: 'v2', path: '0002', title: '第二卷' }),
  node({ id: 'c3', parent_id: 'v2', path: '0002.0001', depth: 1, ordinal: 3,
    kind: 'chapter', title: '第三章', word_count: 140 }),
  node({ id: 'r1', path: '0003', title: '根上的独立章', ordinal: 4, kind: 'chapter',
    word_count: 150 }),
]

function mountTree() {
  return mount(StudioTree, { props: { tree: TREE, workId: 'w_1' }, attachTo: document.body })
}

/** 菜单项按文案取：DOM 里此时只有这一次打开的那一份 */
function item(label) {
  return [...document.body.querySelectorAll('.ctx-item')]
    .find((b) => b.querySelector('.ctx-label')?.textContent.trim() === label)
}

async function openMenu(w, id) {
  const row = w.findAll('.tree-row').find((r) => r.text().includes(
    TREE.find((x) => x.id === id).title))
  await row.trigger('contextmenu', { clientX: 40, clientY: 40 })
  await flushPromises()
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
})

it('卷也有上移/下移：换的是整卷，不是只挪一行', async () => {
  const w = mountTree()
  await openMenu(w, 'v1')

  expect(item('上移').disabled).toBe(true)      // 第一个卷
  expect(item('下移').disabled).toBe(false)
  expect(item('下移').textContent).toContain('整卷连同章节一起换')

  item('下移').click()
  await flushPromises()
  expect(w.emitted('move')).toHaveLength(1)
  expect(w.emitted('move')[0][0].id).toBe('v1')
  expect(w.emitted('move')[0][1]).toBe('down')
})

it('首/末按「同一父节点 + 同一类」分组算：卷与卷比，章与章比', async () => {
  const w = mountTree()
  await openMenu(w, 'v2')
  expect(item('上移').disabled).toBe(false)
  expect(item('下移').disabled).toBe(true)      // 最后一个卷

  // 根上只有这一个叶子：它在「根 | 叶子」那一组，不能因为它是整棵树的最后一行
  // 就说它「下移」可用（后面没有兄弟）
  await openMenu(w, 'r1')
  expect(item('上移').disabled).toBe(true)
  expect(item('下移').disabled).toBe(true)
})

it('章能移出到顶层：一本书只有卷时拖拽没地方落', async () => {
  const w = mountTree()
  await openMenu(w, 'c1')

  expect(item('移出到顶层')).toBeTruthy()
  item('移出到顶层').click()
  await flushPromises()

  const [moved, target, placement] = w.emitted('reparent')[0]
  expect(moved.id).toBe('c1')
  expect(target).toBe(null)
  expect(placement).toEqual({ mode: 'inside' })
})

it('已经挂在根上的章没有「移出到顶层」：那一项会是个空操作', async () => {
  const w = mountTree()
  await openMenu(w, 'r1')
  expect(item('移出到顶层')).toBeFalsy()
})

it('卷的菜单里没有「移出到顶层」与拆分/合并：那些只对章/节成立', async () => {
  const w = mountTree()
  await openMenu(w, 'v1')
  expect(item('移出到顶层')).toBeFalsy()
  expect(item('在光标处拆分')).toBeFalsy()
  expect(item('与下一节合并')).toBeFalsy()
  expect(item('删除')).toBeTruthy()
})
