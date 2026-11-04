/**
 * 缺口 6 的 store 层：连载节奏数据的拉取与每日目标的写入。书架上的节奏条
 * 渲染与 title 文案是视图行为（happy-dom 量不到悬浮提示），验证收口在
 * 请求路径与状态落点。
 */
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const calls = []
let reply = null
vi.mock('../src/utils/api', () => ({
  apiFetch: (url, opts = {}) => {
    calls.push({ url, opts })
    return Promise.resolve(reply ?? { ok: true, status: 200, json: () => Promise.resolve({}) })
  },
}))

const { useStudioStore } = await import('../src/stores/studio')

function ok(body) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) }
}

beforeEach(() => {
  setActivePinia(createPinia())
  calls.length = 0
  reply = null
})

it('loadPipeline 落在 pipeline：节奏条各字段原样进 store，前端不再换算一遍', async () => {
  const s = useStudioStore()
  reply = ok({
    days: 14, stock_words: 2500, stock_nodes: 3, window_chars: 700,
    pace: 50, pace_source: 'history', target: 0, days_left: 50,
  })
  await s.loadPipeline()
  expect(calls[0].url).toBe('/studio/stats/pipeline')
  expect(s.pipelineStats.days_left).toBe(50)
  expect(s.pipelineStats.pace_source).toBe('history')
})

it('接口没就绪就保持 null：节奏条宁可整条不出现，也不摆个假天数', async () => {
  const s = useStudioStore()
  reply = { ok: false, status: 500, json: () => Promise.resolve({}) }
  await s.loadPipeline()
  expect(s.pipelineStats).toBe(null)
})

it('每日目标走设置通道：PUT 的键与值原样出去，settings 就地合并', async () => {
  const s = useStudioStore()
  reply = ok({ key: 'daily_target_words', value: '2000' })
  expect(await s.saveSetting('daily_target_words', '2000')).toBe(true)
  expect(calls[0].url).toBe('/studio/settings/daily_target_words')
  expect(calls[0].opts.method).toBe('PUT')
  expect(JSON.parse(calls[0].opts.body)).toEqual({ value: '2000' })
  expect(s.settings.daily_target_words).toBe('2000')
})

it('目标保存失败要说实话：返回 false 并留下错误，不假装存上了', async () => {
  const s = useStudioStore()
  reply = { ok: false, status: 400, json: () => Promise.resolve({ detail: '不支持的设置项' }) }
  expect(await s.saveSetting('daily_target_words', '999999')).toBe(false)
  expect(s.error).not.toBe('')
  expect(s.settings.daily_target_words).toBeUndefined()
})
