/**
 * store.importNodes：整本书怎么分块发给 `/nodes/batch`。
 * 盯四件在「逐节发」那一版里踩过或会踩的事：
 *   1) 一块 100 节而不是 250 个请求——逐节发实测 2000 章要 39 秒；
 *   2) 每一块都带 kind / parent_id，落库位置不能块间漂；
 *   3) 一块失败就停：后端一块一个事务，往后白发只会把半本书越拖越乱；
 *   4) 被跳过的节要报得出是第几节（后端只给了块内下标），而且一节都没成时
 *      不该再发一次建索引。
 */
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const calls = []
let handlers = {}

vi.mock('../src/utils/api', () => ({
  apiFetch: (url, opts = {}) => {
    calls.push({ url, opts })
    const key = `${opts.method || 'GET'} ${url}`
    // 没登记的一律 404：多打一条少打一条都会被下面的 calls 断言逮住
    const h = handlers[key] || (() => Promise.resolve(http(404, { detail: `没登记的请求 ${key}` })))
    return h()
  },
}))

const { useStudioStore } = await import('../src/stores/studio')

function http(status, body) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) }
}

function secs(n) {
  return Array.from({ length: n }, (_, i) => ({ title: `第 ${i + 1} 章`, content: `正文 ${i + 1}` }))
}

/** 只看发给后端的那几条 batch，把收尾的 tree/stats/index 滤掉 */
function batchCalls() {
  return calls.filter((c) => c.url.endsWith('/nodes/batch'))
}

beforeEach(() => {
  setActivePinia(createPinia())
  calls.length = 0
  handlers = {
    'POST /studio/works/w_1/nodes/batch': () => Promise.resolve(http(200, { created: 100, failed: [], first_id: 'n_a' })),
    'GET /studio/works/w_1/tree': () => Promise.resolve(http(200, [])),
    'GET /studio/works/w_1/stats': () => Promise.resolve(http(200, { nodes: 0 })),
    'POST /studio/works/w_1/index': () => Promise.resolve(http(200, { chunks_total: 3, nodes: 1, skipped: 0 })),
    'GET /studio/works/w_1/index/status': () => Promise.resolve(http(200, {})),
  }
})

it('250 节分三块发：不再是一节一个请求', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1', title: '夜航船' }
  const seen = []
  const res = await s.importNodes(secs(250), { kind: 'chapter', parentId: null }, (d, t) => seen.push([d, t]))

  const sent = batchCalls()
  expect(sent).toHaveLength(3)
  expect(sent.map((c) => JSON.parse(c.opts.body).sections.length)).toEqual([100, 100, 50])
  expect(res.created).toBe(300)          // 替身每块都报 created:100：累加口径要看得懂
  expect(res.failed).toBe(0)
  expect(res.firstId).toBe('n_a')        // 只取第一块的 first_id
  expect(res.stopped).toBe(false)
  expect(seen).toEqual([[100, 250], [200, 250], [250, 250]])
})

it('每一块都带落库位置：kind 与 parent_id 不能块间漂', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  await s.importNodes(secs(120), { kind: 'section', parentId: 'v_9' })
  for (const c of batchCalls()) {
    const b = JSON.parse(c.opts.body)
    expect(b.kind).toBe('section')
    expect(b.parent_id).toBe('v_9')
  }
  expect(JSON.parse(batchCalls()[0].opts.body).sections[0]).toEqual({ title: '第 1 章', content: '正文 1' })
})

it('第二块被拒就停：剩下的块不发，半本书停在哪儿说得出来', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  let n = 0
  handlers['POST /studio/works/w_1/nodes/batch'] = () => {
    n += 1
    return Promise.resolve(n === 1
      ? http(200, { created: 100, failed: [], first_id: 'n_a' })
      : http(400, { detail: '一次最多 200 节，请分批发' }))
  }
  const res = await s.importNodes(secs(300))

  expect(batchCalls()).toHaveLength(2)   // 第三块白发没意义
  expect(res.created).toBe(100)
  expect(res.failed).toBe(200)           // 这一块的 100 + 后面根本没发的 100
  expect(res.stopped).toBe(true)
  expect(res.reasons).toEqual(['第 101 节起没导进去：一次最多 200 节，请分批发'])
})

it('后端逐条报的坏节：用全局节号替它报名，不甩一个「1 节没成功」', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  let n = 0
  handlers['POST /studio/works/w_1/nodes/batch'] = () => {
    n += 1
    return Promise.resolve(http(200, n === 1
      ? { created: 100, failed: [], first_id: 'n_a' }
      : { created: 0, failed: [{ index: 1, title: '', reason: '标题为空' }], first_id: '' }))
  }
  const res = await s.importNodes(secs(120))

  expect(res.created).toBe(100)
  expect(res.failed).toBe(1)
  expect(res.reasons).toEqual(['第 102 节：标题为空'])   // 块内下标 1 + 全局起点 100 + 1
  expect(res.stopped).toBe(false)                        // 跳过坏节不算整块失败，继续发
})

it('一节都没成时不建索引：白发的请求也要被看见', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  handlers['POST /studio/works/w_1/nodes/batch'] = () => Promise.resolve(http(200, {
    created: 0, failed: [{ index: 0, title: '第一章', reason: '标题为空' }], first_id: '',
  }))
  const res = await s.importNodes(secs(1))

  expect(res.failed).toBe(1)
  expect(res.reasons).toEqual(['第一章：标题为空'])      // 有标题就用它自己的
  expect(calls.some((c) => c.url.endsWith('/index'))).toBe(false)
  expect(calls.map((c) => c.url)).toEqual([
    '/studio/works/w_1/nodes/batch',
    '/studio/works/w_1/tree',
    '/studio/works/w_1/stats',
  ])
})

it('没选中作品直接返回空：不发任何请求', async () => {
  const s = useStudioStore()
  s.work = null
  const res = await s.importNodes(secs(3))
  expect(calls).toEqual([])
  expect(res).toEqual({ created: 0, failed: 0, firstId: '', reasons: [], stopped: false })
})
