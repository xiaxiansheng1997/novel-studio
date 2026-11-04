/**
 * 缺口 5 的 store 层：正文写入要带上自己那份的时间戳，409 撞闸时把服务端
 * 整份接进 saveConflict 交给冲突条。冲突条 UI 与 meta 轮询是视图行为，
 * happy-dom 量不到定时器和渲染链路，验证收口在请求体形状与响应消费。
 */
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const calls = []
let replies = []
vi.mock('../src/utils/api', () => ({
  apiFetch: (url, opts = {}) => {
    calls.push({ url, opts })
    return Promise.resolve(replies.shift() ?? ok(nodeWith()))
  },
}))

const { useStudioStore } = await import('../src/stores/studio')

function ok(body) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) }
}

function nodeWith(over = {}) {
  return {
    id: 'n_1', work_id: 'w_1', title: '第一节',
    content: '旧正文', word_count: 3, status: 'draft', updated_at: 1000.5, ...over,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  calls.length = 0
  replies = []
})

function bodyOf(i = 0) {
  return JSON.parse(calls[i].opts.body)
}

it('保存正文必带自己那份的时间戳：base_updated_at 是乐观锁的凭证', async () => {
  const s = useStudioStore()
  s.node = nodeWith()
  replies = [ok(nodeWith({ content: '新正文', updated_at: 1001.2 }))]
  expect(await s.saveNode({ content: '新正文' })).toBe(true)
  expect(bodyOf().base_updated_at).toBe(1000.5)
})

it('节点上没有时间戳不硬编一个假版本：宁可不拦也不误拦', async () => {
  const s = useStudioStore()
  s.node = { id: 'n_1', content: '旧正文' }
  replies = [ok(nodeWith())]
  await s.saveNode({ content: '新正文' })
  expect(bodyOf().base_updated_at).toBeUndefined()
})

it('只改标题不带 base：冲突闸只管正文，别管闲事', async () => {
  const s = useStudioStore()
  s.node = nodeWith()
  replies = [ok(nodeWith({ title: '新标题' }))]
  await s.saveNode({ title: '新标题' })
  expect(bodyOf().base_updated_at).toBeUndefined()
  expect(bodyOf().content).toBeUndefined()
})

it('409 撞闸：服务端整份进 saveConflict，保存返回 false、error 不背锅', async () => {
  const s = useStudioStore()
  s.node = nodeWith()
  const server = nodeWith({ content: '对方的版本', updated_at: 1002 })
  replies = [{
    ok: false, status: 409,
    json: () => Promise.resolve({ detail: { conflict: true, server } }),
  }]
  expect(await s.saveNode({ content: '我的' })).toBe(false)
  expect(s.saveConflict).toEqual(server)
  expect(s.error).toBe('')
})

it('撞闸后强存成功：ignore_conflict 随请求出去，saveConflict 翻页清零', async () => {
  const s = useStudioStore()
  s.node = nodeWith()
  const server = nodeWith({ content: '对方的版本', updated_at: 1002 })
  replies = [
    { ok: false, status: 409, json: () => Promise.resolve({ detail: { conflict: true, server } }) },
    ok(nodeWith({ content: '我的', updated_at: 1003 })),
    ok([]),   // snapshot 才拉版本，这里带 snapshot 验证清冲突也走一遍
  ]
  await s.saveNode({ content: '我的' }, { snapshot: true, source: 'human' })
  expect(s.saveConflict).toEqual(server)
  expect(await s.saveNode({ content: '我的' }, { snapshot: true, source: 'human', ignoreConflict: true })).toBe(true)
  expect(bodyOf(1).ignore_conflict).toBe(true)
  expect(s.saveConflict).toBe(null)
})

it('定稿闸的 409（没有 conflict 键）照旧走 errDetail：两路 409 不相混', async () => {
  const s = useStudioStore()
  s.node = nodeWith()
  replies = [{
    ok: false, status: 409,
    json: () => Promise.resolve({ detail: { message: '还有硬告警', open_errors: 2 } }),
  }]
  expect(await s.saveNode({ status: 'final' }, { force: true })).toBe(false)
  expect(s.error).toBe('还有硬告警')
  expect(s.saveConflict).toBe(null)
})
