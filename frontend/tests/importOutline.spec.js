/**
 * store.importOutline：一级标题建卷、二级标题挂在卷下。
 * 盯四件平铺导入做不到、做错又很难发现的事：
 *   1) 卷和它下面的章按阅读顺序建，章带 parent_id、卷自己不带（容器不进正文）；
 *   2) 卷首语不能丢：一级标题自己那几段话补成这一卷的第一节；
 *   3) 一卷没建起来就停，而且「没导的节」的计数要对得上人头；
 *   4) 索引整本只建一次——按卷收尾会把几千章重建好几趟。
 */
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const calls = []
let handlers = {}
let volSeq = 0

vi.mock('../src/utils/api', () => ({
  apiFetch: (url, opts = {}) => {
    calls.push({ url, opts })
    const key = `${opts.method || 'GET'} ${url}`
    // 没登记的一律 404：多打一条少打一条都会被下面的 calls 断言逮住
    const h = handlers[key] || (() => Promise.resolve(http(404, { detail: `没登记的请求 ${key}` })))
    return h(opts)
  },
}))

const { useStudioStore } = await import('../src/stores/studio')

function http(status, body) {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) }
}

const kid = (title, content = '正文') => ({ title, content, children: [] })

function vol(title, kids, content = '') {
  return { title, content, children: kids }
}

/** 只看建卷与灌章那几条，把收尾的 tree/stats/index 滤掉 */
function writeCalls() {
  return calls.filter((c) => c.url.endsWith('/nodes') || c.url.endsWith('/nodes/batch'))
}

beforeEach(() => {
  setActivePinia(createPinia())
  calls.length = 0
  volSeq = 0
  handlers = {
    // 建卷：每卷给一个新 id，好断言「章挂在自己那一卷下面」
    'POST /studio/works/w_1/nodes': () => Promise.resolve(http(200, { id: `v_${++volSeq}` })),
    // 灌章：报回来的 created 跟这一块实际发了几节一致
    'POST /studio/works/w_1/nodes/batch': (opts) => {
      const body = JSON.parse(opts.body)
      return Promise.resolve(http(200, {
        created: body.sections.length, failed: [], first_id: 'n_first',
      }))
    },
    'GET /studio/works/w_1/tree': () => Promise.resolve(http(200, [])),
    'GET /studio/works/w_1/stats': () => Promise.resolve(http(200, { nodes: 0 })),
    'POST /studio/works/w_1/index': () => Promise.resolve(http(200, { chunks_total: 3, nodes: 3, skipped: 0 })),
    'GET /studio/works/w_1/index/status': () => Promise.resolve(http(200, {})),
  }
})

it('卷先建、章后灌：每批章都挂在自己那一卷下面', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  const outline = [
    vol('第一卷 起雾', [kid('第一章 灯下'), kid('第二章 船')]),
    vol('第二卷 逆流', [kid('第三章 桥')]),
  ]
  const seen = []
  const res = await s.importOutline(outline, { kind: 'chapter' }, (d, t) => seen.push([d, t]))

  expect(writeCalls().map((c) => c.url.split('/w_1/')[1])).toEqual([
    'nodes', 'nodes/batch', 'nodes', 'nodes/batch',
  ])
  const first = JSON.parse(writeCalls()[0].opts.body)
  expect(first).toEqual({ kind: 'volume', title: '第一卷 起雾', content: '', parent_id: null })

  const b1 = JSON.parse(writeCalls()[1].opts.body)
  const b2 = JSON.parse(writeCalls()[3].opts.body)
  expect(b1.parent_id).toBe('v_1')          // 不是上一卷、不是根
  expect(b1.kind).toBe('chapter')
  expect(b1.sections.map((x) => x.title)).toEqual(['第一章 灯下', '第二章 船'])
  expect(b2.parent_id).toBe('v_2')
  expect(b2.sections.map((x) => x.title)).toEqual(['第三章 桥'])

  expect(res.volumes).toBe(2)
  expect(res.created).toBe(3)
  expect(res.failed).toBe(0)
  expect(res.firstId).toBe('n_first')       // firstId 只认章：卷选不中
  expect(seen).toEqual([[2, 3], [3, 3]])    // 进度按节算，不把卷算进去
})

it('卷首语不能丢：一级标题自带正文的，补成这一卷的第一节', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  const outline = [vol('第一卷', [kid('第一章')], '这一卷讲的是他出海那年。')]
  const res = await s.importOutline(outline, { kind: 'chapter' })

  const sent = JSON.parse(writeCalls()[1].opts.body).sections
  expect(sent.map((x) => x.title)).toEqual(['第一卷（卷首）', '第一章'])
  expect(sent[0].content).toBe('这一卷讲的是他出海那年。')
  expect(JSON.parse(writeCalls()[0].opts.body).content).toBe('')   // 正文不能抄进卷容器
  expect(res.created).toBe(2)
})

it('根上的独立章不建卷：攒成一批落在根上', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  const outline = [kid('序章'), vol('第一卷', [kid('第一章')])]
  const res = await s.importOutline(outline, { kind: 'chapter' })

  // 根上的章排在卷前面：阅读顺序就是「序章 → 第一卷」，抢在卷前发出去才对
  expect(writeCalls().map((c) => c.url.split('/w_1/')[1])).toEqual(['nodes/batch', 'nodes', 'nodes/batch'])
  const b1 = JSON.parse(writeCalls()[0].opts.body)
  expect(b1.parent_id).toBe(null)
  expect(b1.sections.map((x) => x.title)).toEqual(['序章'])
  expect(JSON.parse(writeCalls()[2].opts.body).parent_id).toBe('v_1')
  expect(res.volumes).toBe(1)
  expect(res.created).toBe(2)
})

it('一卷没建起来就停：后面的卷不发，没导的节都算没成功', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  let n = 0
  handlers['POST /studio/works/w_1/nodes'] = () => {
    n += 1
    return Promise.resolve(n === 1
      ? http(200, { id: `v_${n}` })
      : http(400, { detail: '标题太长' }))
  }
  const outline = [
    vol('第一卷', [kid('第一章'), kid('第二章')]),
    vol('第二卷', [kid('第三章')]),
    vol('第三卷', [kid('第四章')]),
  ]
  const res = await s.importOutline(outline, { kind: 'chapter' })

  expect(writeCalls().map((c) => c.url.split('/w_1/')[1])).toEqual(['nodes', 'nodes/batch', 'nodes'])
  expect(res.volumes).toBe(1)
  expect(res.created).toBe(2)
  expect(res.failed).toBe(2)               // 第二卷的 1 节 + 第三卷的 1 节，不能把第二卷算两遍
  expect(res.stopped).toBe(true)
  expect(res.reasons).toEqual(['第二卷：这一卷没建起来，它下面的 1 节没导'])
})

it('索引整本只建一次：按卷收尾会把几千章重建好几趟', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  await s.importOutline([
    vol('第一卷', [kid('第一章'), kid('第二章')]),
    vol('第二卷', [kid('第三章')]),
  ], { kind: 'chapter' })

  expect(calls.filter((c) => c.url.endsWith('/index'))).toHaveLength(1)
  expect(calls.filter((c) => c.url.endsWith('/tree'))).toHaveLength(1)
  expect(calls.filter((c) => c.url.endsWith('/stats'))).toHaveLength(1)
})

it('一节都没成时不建索引：卷建了也白搭', async () => {
  const s = useStudioStore()
  s.work = { id: 'w_1' }
  handlers['POST /studio/works/w_1/nodes/batch'] = () => Promise.resolve(http(200, {
    created: 0, failed: [{ index: 0, title: '', reason: '标题为空' }], first_id: '',
  }))
  const res = await s.importOutline([vol('第一卷', [kid('第一章')])], { kind: 'chapter' })

  expect(res.created).toBe(0)
  expect(res.failed).toBe(1)
  expect(calls.some((c) => c.url.endsWith('/index'))).toBe(false)
})
