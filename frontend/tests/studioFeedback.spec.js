/**
 * 缺口 4 的埋点接线：「采用」时 run_id 必须随 PATCH 落进版本行——
 * 控制台的采纳率靠 node_versions.run_id 这根线，断了信号就是全零。
 * 只验请求体形状：落库与统计口径是 pytest 那一侧的事。
 */
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const calls = []
vi.mock('../src/utils/api', () => ({
  apiFetch: (url, opts = {}) => {
    calls.push({ url, opts })
    // PATCH 的响应会被当成新 node 整包收下：给一个最小但形状正确的节点
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({
        id: 'n_1', work_id: 'w_1', title: '第一节', content: '', word_count: 0, status: 'draft',
      }),
    })
  },
}))

const { useStudioStore } = await import('../src/stores/studio')

beforeEach(() => {
  setActivePinia(createPinia())
  calls.length = 0
})

function patchBody() {
  const p = calls.find((c) => c.opts.method === 'PATCH')
  expect(p).toBeTruthy()
  return JSON.parse(p.opts.body)
}

it('采用生成结果：run_id 从 SSE meta 一路带到 PATCH 体', async () => {
  const s = useStudioStore()
  s.node = { id: 'n_1', content: '旧正文' }
  s.genMeta = { run_id: 'r_abc', model: 'alpha' }
  const ok = await s.adoptGenerated({ append: false, source: 'ai_draft', text: '新正文' })
  expect(ok).toBe(true)

  const body = patchBody()
  expect(body.content).toBe('新正文')
  expect(body.snapshot).toBe(true)
  expect(body.snapshot_source).toBe('ai_draft')
  expect(body.run_id).toBe('r_abc')
  // 采用成功即清预览与 meta：下一次 PATCH 不该复用这根旧线
  expect(s.genMeta).toBe(null)
})

it('逐段部分采用也归因同一次生成：text 传了照样带 run_id', async () => {
  const s = useStudioStore()
  s.node = { id: 'n_1', content: '旧正文' }
  s.genMeta = { run_id: 'r_seg' }
  await s.adoptGenerated({ append: false, source: 'ai_draft', text: '只要第一段' })
  const body = patchBody()
  expect(body.content).toBe('只要第一段')
  expect(body.run_id).toBe('r_seg')
})

it('meta 缺失不硬编：没有 run_id 就不进请求体，PATCH 照常成功', async () => {
  const s = useStudioStore()
  s.node = { id: 'n_1', content: '旧正文' }
  const ok = await s.adoptGenerated({ append: false, source: 'ai_draft', text: '新正文' })
  expect(ok).toBe(true)
  expect(patchBody().run_id).toBeUndefined()
})

it('普通保存（不带 snapshot）永不出现 run_id：自动保存不该被当成采纳', async () => {
  const s = useStudioStore()
  s.node = { id: 'n_1', content: '旧正文' }
  await s.saveNode({ content: '敲了几个字' })
  const body = patchBody()
  expect(body.snapshot).toBe(false)
  expect(body.run_id).toBeUndefined()
  expect(body.snapshot_source).toBeUndefined()
})
