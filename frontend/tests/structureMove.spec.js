/**
 * store.moveNode：换位之后当前那一节要重新取一次。
 *
 * 同层换位只交换两个兄弟的 path 与 ordinal，听起来与「当前打开的那一节」无关；
 * 但换的是「卷」的时候，后端按新树序把全书章号 1..N 重排一遍——当前开着的
 * 那一节即便不是被移动的那一卷，章号也变了。摘要、一致性检查、生成提示都按
 * 章号取上下文，界面停在旧号上写着，写出来的东西就会引用错章。
 *
 * 另外两条口径：
 *   - `moved: false`（已到边界）不是错误，不该白拉一次树；
 *   - 没选任何节时只重拉树，不去 select 一个不存在的目标。
 */
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const calls = []
let handlers = {}

vi.mock('../src/utils/api', () => ({
  apiFetch: (url, opts = {}) => {
    calls.push({ url, opts })
    const key = `${opts.method || 'GET'} ${url}`
    const h = handlers[key] || (() => Promise.resolve(http(404, { detail: `没登记的请求 ${key}` })))
    return h()
  },
}))

const { useStudioStore } = await import('../src/stores/studio')

function http(status, body) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) }
}

const TREE = [
  { id: 'v1', path: '0001', title: '第一卷', ordinal: null, parent_id: null },
  { id: 'n_cur', path: '0001.0001', title: '第一章', ordinal: 1, parent_id: 'v1' },
  { id: 'v2', path: '0002', title: '第二卷', ordinal: null, parent_id: null },
  { id: 'n_b', path: '0002.0001', title: '第三章', ordinal: 2, parent_id: 'v2' },
]

/** 第二卷换到最前之后：原来的第 1 章变成第 3 章 */
const TREE_AFTER = [
  { id: 'v2', path: '0001', title: '第二卷', ordinal: null, parent_id: null },
  { id: 'n_b', path: '0001.0001', title: '第三章', ordinal: 1, parent_id: 'v2' },
  { id: 'v1', path: '0002', title: '第一卷', ordinal: null, parent_id: null },
  { id: 'n_cur', path: '0002.0001', title: '第一章', ordinal: 3, parent_id: 'v1' },
]

function boot() {
  const s = useStudioStore()
  // 走 $patch：IDE 把 setup store 的属性推成只读，直接赋值会标红（运行时两种写法等价）
  s.$patch({
    work: { id: 'w_1', title: '夜航船' },
    node: { id: 'n_cur', title: '第一章', ordinal: 1 },
    tree: TREE,
  })
  return s
}

beforeEach(() => {
  setActivePinia(createPinia())
  calls.length = 0
  handlers = {
    'POST /studio/nodes/v2/move': () => Promise.resolve(http(200, {
      moved: true, direction: 'up', ordinal: null,
      swapped_with_title: '第一卷', renumbered: true,
    })),
    'GET /studio/works/w_1/tree': () => Promise.resolve(http(200, TREE_AFTER)),
    'GET /studio/nodes/n_cur': () => Promise.resolve(http(200,
      { id: 'n_cur', title: '第一章', ordinal: 3, content: '甲'.repeat(40) })),
    'GET /studio/nodes/n_cur/versions': () => Promise.resolve(http(200, [])),
    'GET /studio/works/w_1/issues?node_id=n_cur': () => Promise.resolve(http(200, [])),
  }
})

it('整卷换位后重选当前那一节：章号从 1 变 3 要带进编辑器', async () => {
  const s = boot()
  const res = await s.moveNode('v2', 'up')

  expect(res.moved).toBe(true)
  expect(s.node.ordinal).toBe(3)      // 移动的不是当前节，但号变了
  expect(s.tree[0].id).toBe('v2')     // 树也重拉了
  const urls = calls.map((c) => c.url)
  expect(urls).toContain('/studio/works/w_1/tree')
  expect(urls).toContain('/studio/nodes/n_cur')
})

it('移动的就是当前节：也要重选，章号同样换位重排', async () => {
  const s = boot()
  handlers['POST /studio/nodes/n_cur/move'] = () => Promise.resolve(http(200, {
    moved: true, direction: 'down', ordinal: 2, swapped_with_title: '第三章',
  }))

  await s.moveNode('n_cur', 'down')

  expect(s.node.ordinal).toBe(3)
  expect(calls.some((c) => c.url === '/studio/nodes/n_cur')).toBe(true)
})

it('到边界不是错误：不重拉树、不重选，reason 原样给界面', async () => {
  const s = boot()
  handlers['POST /studio/nodes/v2/move'] = () => Promise.resolve(http(200, {
    moved: false, reason: '已到边界',
  }))

  const res = await s.moveNode('v2', 'up')

  expect(res).toEqual({ moved: false, reason: '已到边界' })
  expect(calls.map((c) => c.url)).toEqual(['/studio/nodes/v2/move'])
  expect(s.error).toBe('')
})

it('接口失败：落一句人话在 error 上，不做任何收尾', async () => {
  const s = boot()
  handlers['POST /studio/nodes/v2/move'] = () => Promise.resolve(http(400, { detail: '方向只能是 up / down' }))

  const res = await s.moveNode('v2', 'up')

  expect(res.moved).toBe(false)
  expect(s.error).toBe('移动失败')
  expect(calls.map((c) => c.url)).toEqual(['/studio/nodes/v2/move'])
})

it('没选任何节时只重拉树：不去 select 一个不存在的目标', async () => {
  const s = boot()
  s.$patch({ node: null })

  await s.moveNode('v2', 'up')

  expect(s.tree[0].id).toBe('v2')
  expect(calls.map((c) => c.url)).toEqual([
    '/studio/nodes/v2/move',
    '/studio/works/w_1/tree',
  ])
})
