/**
 * CraftPanel（拆解页）：请求形状与三态——入队 / 402 闸门 / 拷贝与抽模板。
 * apiFetch 与 store 都换成替身：端点逻辑在 test_craft.py 里，这里只测
 * 「发出什么、拿回来的东西怎么摆」。最要紧的两条：
 *   1) 带 body 的请求必须显式带 Content-Type（真实 fetch 不自动加，漏了 FastAPI 422）；
 *   2) 402 后勾「忽略预算」必须原样重发、带 ignore_budget=true，而不是前端自己放行。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const apiFetch = vi.hoisted(() => vi.fn())
const store = vi.hoisted(() => ({
  work: { id: 'w1', title: '测试书', kind: 'novel' },
  activeWorkId: 'w1',
  genModel: 'deepseek-chat',
  modelGroups: [
    { key: 'deepseek', name: 'DeepSeek', models: [{ id: 'deepseek-chat', label: 'DeepSeek Chat' }] },
  ],
  // 范围下拉的选项源：ordinal 为 null 的是容器（卷）
  tree: [
    { id: 'v1', title: '第一卷', path: '0001', ordinal: null },
    { id: 'v2', title: '第二卷', path: '0002', ordinal: null },
    { id: 'c1', title: '第1章', path: '0001.0001', ordinal: 1 },
    { id: 'c2', title: '第2章', path: '0001.0002', ordinal: 2 },
  ],
  setGenModel: vi.fn(),
  loadSources: vi.fn(async () => {}),
  // 并行路数只有队列状态接口知道：替身走同一套路由，返回后端那份状态
  loadQueue: vi.fn(async () => {
    const r = await apiFetch('/studio/queue/status?work_id=w1')
    return r.ok ? await r.json() : null
  }),
}))

vi.mock('../src/utils/api', () => ({ apiFetch }))
vi.mock('../src/stores/studio', () => ({ useStudioStore: () => store }))

// eslint-disable-next-line import/first
import CraftPanel from '../src/components/CraftPanel.vue'

const okJson = (body = {}) => ({ ok: true, status: 200, json: async () => body })
const errJson = (detail, status = 400) => ({ ok: false, status, json: async () => ({ detail }) })

/** 后端已按 全书 → 卷 → 章 排好：前端不再重排，这里按同样顺序喂 */
const CARDS = () => [
  {
    id: 'cr_book', level: 'book', title: '测试书', note: '',
    text: '结构类型：三幕式\n幕结构：铺垫 → 升级 → 收束',
    data: { structure_type: '三幕式', acts: [{ name: '铺垫' }, { name: '升级' }, { name: '收束' }] },
  },
  {
    id: 'cr_v1', level: 'volume', title: '第一卷', note: '',
    text: '弧线：从忍让到反击',
    data: { arc: '从忍让到反击', foreshadows: [{}, {}] },
  },
  {
    id: 'cr_c1', level: 'chapter', title: '第1章', note: '正文 2 万字，只拆了前 1.2 万字',
    text: '冲突：主角与房东争租约',
    data: { scenes: [{}, {}], hook: '律师函出现' },
  },
]

const craftFixture = (over = {}) => ({
  analyses: CARDS(),
  progress: {},
  total_chapters: 3,
  analysed_chapters: 1,
  ...over,
})

/** 按「方法 + URL」分发替身响应；没登记的请求直接失败，免得静默吞掉一条真实调用 */
function routeApi(handlers = {}) {
  apiFetch.mockImplementation(async (url, opts = {}) => {
    const key = `${opts.method || 'GET'} ${url}`
    if (handlers[key]) return handlers[key](url, opts)
    if (key === 'GET /studio/works/w1/craft') return okJson(craftFixture())
    if (key === 'GET /studio/works') {
      return okJson([{ id: 'w1', title: '测试书' }, { id: 'w2', title: '新书' }])
    }
    return errJson(`没 mock 的请求：${key}`, 404)
  })
}

async function mountPanel(handlers) {
  routeApi(handlers)
  const w = mount(CraftPanel)
  await flushPromises()
  return w
}

const byText = (w, text) => w.findAll('button').find((b) => b.text().includes(text))

beforeEach(() => {
  apiFetch.mockReset()
  store.setGenModel.mockClear()
  store.loadSources.mockClear()
  store.loadQueue.mockClear()
})

it('挂载即拉：卡片按后端顺序渲染，截断警告与进度数字都在', async () => {
  const w = await mountPanel()
  expect(w.findAll('.card-item')).toHaveLength(3)
  // 后端已排好 book→volume→chapter；前端若再排一次，倒金字塔会翻回来
  expect(w.findAll('.lv-badge').map((n) => n.text())).toEqual(['全书', '卷', '章'])
  expect(w.findAll('.card-item')[0].classes()).toContain('lv-book')
  expect(w.text()).toContain('已拆 1/3 章')
  expect(w.find('.card-note').text()).toContain('只拆了前 1.2 万字')
  // 默认目标书是本书
  expect(w.get('.pick-row select').element.value).toBe('w1')
})

it('开始拆解：请求体与 Content-Type 齐全，响应回填成「已入队」一行', async () => {
  const w = await mountPanel({
    'POST /studio/works/w1/craft': () => okJson({
      queued: 4, deduped: 0, chapters: 3, note: '',
      estimate: { prompt_chars_total: 12000, priced: false }, queue: {},
    }),
  })
  await byText(w, '开始拆解').trigger('click')
  await flushPromises()

  const call = apiFetch.mock.calls.find(([, o]) => o?.method === 'POST')
  expect(call[0]).toBe('/studio/works/w1/craft')
  // 真实 fetch 对字符串 body 不自动加 Content-Type，漏了就是 422（真机栽过）
  expect(call[1].headers).toEqual({ 'Content-Type': 'application/json' })
  expect(JSON.parse(call[1].body)).toEqual({
    model: 'deepseek-chat',
    levels: ['chapter', 'volume', 'book'],
    force: false,
    ignore_budget: false,
    scope_node_id: '',   // 空 = 整本书
  })
  expect(w.text()).toContain('已入队 4 条')
  expect(w.text()).toContain('预计投喂 12,000 字')
})

it('取消逐章后只发卷与书：层级勾选真实进请求', async () => {
  const w = await mountPanel({
    'POST /studio/works/w1/craft': () => okJson({
      queued: 2, deduped: 0, chapters: 0, note: '没勾章级：卷/全书若发现缺章会失败，等章拆完点重试',
      estimate: { prompt_chars_total: 8000, priced: false }, queue: {},
    }),
  })
  await w.findAll('.level-row input[type="checkbox"]')[0].setValue(false)
  await byText(w, '开始拆解').trigger('click')
  await flushPromises()

  const call = apiFetch.mock.calls.find(([, o]) => o?.method === 'POST')
  expect(JSON.parse(call[1].body).levels).toEqual(['volume', 'book'])
  expect(w.text()).toContain('等章拆完点重试')
})

it('选卷后只拆这一支：请求带 scope_node_id，全书勾选被灰掉并移除', async () => {
  const w = await mountPanel({
    'POST /studio/works/w1/craft': () => okJson({
      queued: 3, deduped: 0, chapters: 2,
      note: '只拆《第一卷》这一支：全书级等各卷拆完再单独点',
      estimate: { prompt_chars_total: 6000, priced: false }, queue: {},
    }),
  })
  await w.get('.scope').setValue('v1')
  await flushPromises()

  // 范围提示给出本章数；全书级灰掉——缺卷的架构分析不是结论
  expect(w.text()).toContain('只拆《第一卷》这一支')
  expect(w.text()).toContain('2 章')
  const bookBox = w.findAll('.level-row input[type="checkbox"]')[2]
  expect(bookBox.element.disabled).toBe(true)

  await byText(w, '开始拆解').trigger('click')
  await flushPromises()

  const call = apiFetch.mock.calls.find(([, o]) => o?.method === 'POST')
  const body = JSON.parse(call[1].body)
  expect(body.scope_node_id).toBe('v1')
  expect(body.levels).toEqual(['chapter', 'volume'])  // book 被移除，不是靠后端默默吞
  expect(w.text()).toContain('全书级等各卷拆完')
})

it('402：显示服务端原话；勾选忽略后重发带 ignore_budget=true', async () => {
  let posts = 0
  const w = await mountPanel({
    'POST /studio/works/w1/craft': () => {
      posts += 1
      return posts === 1
        ? errJson('预估花 1.2，加上已花的 0.3 共 1.5，超预算上限 1.0：改小范围或确认后带 ignore_budget 重跑', 402)
        : okJson({
          queued: 4, deduped: 0, chapters: 3, note: '',
          estimate: { prompt_chars_total: 12000, priced: true, est_cost: '1.2' }, queue: {},
        })
    },
  })
  await byText(w, '开始拆解').trigger('click')
  await flushPromises()

  expect(w.find('.budget .is-bad').text()).toContain('超预算上限')
  expect(w.text()).not.toContain('已入队')   // 被拦就是被拦，不能假装排上了

  await w.get('.budget input[type="checkbox"]').setValue(true)
  await byText(w, '再来一次').trigger('click')
  await flushPromises()

  const postsCalls = apiFetch.mock.calls.filter(([, o]) => o?.method === 'POST')
  expect(postsCalls).toHaveLength(2)
  expect(JSON.parse(postsCalls[1][1].body).ignore_budget).toBe(true)
  expect(w.text()).toContain('已入队 4 条')
})

it('勾卡 + 换目标书：拷贝请求体准确；拷到别书不刷本书素材计数', async () => {
  const w = await mountPanel({
    'POST /studio/works/w1/craft/apply': () => okJson({
      ok: 2, failed: [], cards: [], target_title: '新书',
    }),
  })
  const boxes = w.findAll('.card-item input[type="checkbox"]')
  await boxes[0].setValue(true)
  await boxes[2].setValue(true)
  await w.get('.pick-row select').setValue('w2')
  await byText(w, '拷到书').trigger('click')
  await flushPromises()

  const call = apiFetch.mock.calls.find(([, o]) => o?.method === 'POST')
  expect(call[0]).toBe('/studio/works/w1/craft/apply')
  expect(call[1].headers).toEqual({ 'Content-Type': 'application/json' })
  expect(JSON.parse(call[1].body)).toEqual({
    target_work_id: 'w2',
    analysis_ids: ['cr_book', 'cr_c1'],
  })
  expect(w.text()).toContain('已拷 2 条进《新书》')
  expect(store.loadSources).not.toHaveBeenCalled()
})

it('全书卡抽模板：内联命名进请求，成功后给出模板库去处', async () => {
  const w = await mountPanel({
    'POST /studio/templates/from-craft/w1': () => okJson({ id: 't1', name: '拆出来的骨架' }),
  })
  await w.get('.card-item.lv-book .card-act button').trigger('click')
  await w.get('.card-item.lv-book .tpl-name').setValue('拆出来的骨架')
  await byText(w, '建模板').trigger('click')
  await flushPromises()

  const call = apiFetch.mock.calls.find(([, o]) => o?.method === 'POST')
  expect(call[0]).toBe('/studio/templates/from-craft/w1')
  expect(call[1].headers).toEqual({ 'Content-Type': 'application/json' })
  expect(JSON.parse(call[1].body)).toEqual({ name: '拆出来的骨架' })
  expect(w.text()).toContain('已建成模板')
})

it('队列有活时按状态摆 pill；没活就不开轮询（组件卸载后不留定时器）', async () => {
  const w = await mountPanel({
    'GET /studio/works/w1/craft': () => okJson(craftFixture({
      progress: { queued: 3, done: 1 },
    })),
  })
  expect(w.text()).toContain('排队 3')
  expect(w.text()).toContain('完成 1')
  expect(w.text()).toContain('任务 4 条')
  w.unmount()
})

it('并行路数来自队列状态：多路在拆时把「N 路并行」说出来', async () => {
  const w = await mountPanel({
    'GET /studio/works/w1/craft': () => okJson(craftFixture({
      progress: { running: 3, queued: 8 },
    })),
    'GET /studio/queue/status?work_id=w1': () => okJson({
      worker: true,
      running: [
        { job_id: 'j1', task_type: 'craft', title: '第1章', elapsed_ms: 5000 },
        { job_id: 'j2', task_type: 'craft', title: '第2章', elapsed_ms: 4200 },
        { job_id: 'j3', task_type: 'craft', title: '第3章', elapsed_ms: 3100 },
      ],
      current: { job_id: 'j1', task_type: 'craft', title: '第1章', elapsed_ms: 5000 },
    }),
  })
  expect(w.text()).toContain('3 路并行')
  expect(store.loadQueue).toHaveBeenCalledTimes(1)
})

it('只有一路在跑就不提并行：反代通道不该被文案骗成多路', async () => {
  const w = await mountPanel({
    'GET /studio/works/w1/craft': () => okJson(craftFixture({
      progress: { running: 1, queued: 8 },
    })),
    // 老口径只有 current：当成一路，不能因为读不到 running 就显示空闲
    'GET /studio/queue/status?work_id=w1': () => okJson({
      worker: true,
      current: { job_id: 'j1', task_type: 'craft', title: '第1章', elapsed_ms: 5000 },
    }),
  })
  expect(w.text()).not.toContain('路并行')
  expect(w.text()).toContain('任务 9 条')
})

it('空闲时不拉队列状态：路数只在确实有任务在跑时才多打一次', async () => {
  const w = await mountPanel()
  expect(store.loadQueue).not.toHaveBeenCalled()
  expect(apiFetch.mock.calls.some(([u]) => String(u).startsWith('/studio/queue/status'))).toBe(false)
  w.unmount()
})
