/**
 * TransformDialog 迭代 D-2 的两块新行为：
 * 1. 跨模型对比：勾选接管候选数、至多 3 家、run 时把 models 原样传出；
 * 2. 逐段混合：段槽对位（含段数不齐的兜底）、点选换段、拼写回文本。
 * store 整个换成 reactive 替身：不起后端，出几个候选是 pytest 的事。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { reactive } from 'vue'

const h = vi.hoisted(() => ({ s: null }))
vi.mock('../src/stores/studio', () => ({ useStudioStore: () => h.s }))

// eslint-disable-next-line import/first
import TransformDialog from '../src/components/TransformDialog.vue'

const MODELS = [
  { id: 'gpt-4o-mini', label: 'GPT 4o mini' },
  { id: 'gpt-4o', label: 'GPT 4o' },
  { id: 'deepseek-chat', label: 'DeepSeek' },
  { id: 'glm-4', label: 'GLM 4' },
  { id: 'kimi', label: 'Kimi' },
]

const SRC = 'O1\n\nO2\n\nO3\n\nO4'

beforeEach(() => {
  h.s = reactive({
    transforming: false,
    transformResult: null,
    error: '',
    allGenModels: MODELS,
    transform: vi.fn(async () => null),
  })
})

/** 走真实开弹窗路径：watch(open) 会把 props.text 灌进可编辑的原文框 */
async function open(extra = {}) {
  const w = mount(TransformDialog, {
    props: { open: false, text: SRC, selection: '', instruction: '', context: '', ...extra },
    global: { stubs: { AppIcon: true } },
  })
  await w.setProps({ open: true })
  await flushPromises()
  return w
}

async function runWith(w, result) {
  h.s.transform.mockImplementation(async () => {
    h.s.transformResult = result
    return result
  })
  await w.get('.run-row .btn-primary').trigger('click')
  await flushPromises()
  return w
}

// ==================== 跨模型对比 ====================

it('可用模型都进 chips（上限 12）：不是下拉，太长会顶掉整弹窗', async () => {
  const w = await open()
  const chips = w.findAll('.chip')
  expect(chips).toHaveLength(5)
  expect(chips[4].text()).toBe('Kimi')
})

it('勾选接管候选数：第 4 家点不上，再点已选的可取消', async () => {
  const w = await open()
  const chips = w.findAll('.chip')
  for (const c of chips.slice(0, 3)) await c.trigger('click')
  expect(chips[2].classes()).toContain('on')
  await chips[3].trigger('click')
  expect(chips[3].classes()).not.toContain('on')
  expect(w.get('select.narrow').element.disabled).toBe(true)
  // 取消全部已选才恢复候选数：还剩一家就不该让人改候选数
  await chips[1].trigger('click')
  await chips[2].trigger('click')
  expect(w.get('select.narrow').element.disabled).toBe(true)
  await chips[0].trigger('click')
  expect(w.get('select.narrow').element.disabled).toBe(false)
})

it('run 把勾选原样传出；没勾就是空数组', async () => {
  const w = await open()
  await w.findAll('.chip')[1].trigger('click')
  await runWith(w, { action: 'rewrite', candidates: [], errors: [] })
  const arg = h.s.transform.mock.calls[0][0]
  expect(arg.models).toEqual(['gpt-4o'])
  // 原文框里是被 watch 灌进来的 props.text
  expect(arg.text).toBe(SRC)
})

it('context 随请求走：加工选区不能脱离全节语境', async () => {
  const w = await open({ context: '前文\n【……选区略……】\n后文' })
  await runWith(w, { action: 'rewrite', candidates: [], errors: [] })
  expect(h.s.transform.mock.calls[0][0].context).toBe('前文\n【……选区略……】\n后文')
})

// ==================== 逐段混合 ====================

const TWO = {
  action: 'rewrite',
  candidates: [
    { variant: 0, model: 'gpt-4o-mini', text: 'A1\n\nA2\n\nA3', chars: 6, elapsed_ms: 1000 },
    { variant: 1, model: 'gpt-4o', text: 'B1\n\nB2', chars: 4, elapsed_ms: 2000 },
  ],
  errors: [],
}

it('默认取候选 1；它没有的尾段退回原文，不静默丢内容', async () => {
  const w = await runWith(await open(), TWO)
  await w.get('.res-bar .link').trigger('click')
  const slots = w.findAll('.mix-slot')
  // 段槽数 = max(原文 4, 候选 3, 候选 2)：尾段照常有行
  expect(slots).toHaveLength(4)
  expect(slots[0].findAll('.mix-opt')[0].classes()).toContain('on')
  expect(slots[0].findAll('.mix-opt')[0].text()).toContain('A1')
  expect(w.text()).toContain('写回 4/4 段')
})

it('点选第 2 段换到候选 2、第 4 段丢弃：写回文本按段序拼装', async () => {
  const w = await runWith(await open(), TWO)
  await w.get('.res-bar .link').trigger('click')
  const slots = w.findAll('.mix-slot')
  // 每行选项顺序：各候选（按候选序）→ 原文 → 丢弃
  await slots[1].findAll('.mix-opt')[1].trigger('click')   // B2
  await slots[3].findAll('.mix-opt').at(-1).trigger('click') // 丢弃（O4 那行）
  expect(w.text()).toContain('写回 3/4 段')
  await w.get('.mix-foot .btn-primary').trigger('click')
  expect(w.emitted('adopt')[0][0]).toBe('A1\n\nB2\n\nA3')
  expect(w.emitted('update:open')[0][0]).toBe(false)
})

it('「全用原文」是逃生门：一键回退不动没原文的行', async () => {
  const w = await runWith(await open(), TWO)
  await w.get('.res-bar .link').trigger('click')
  await w.findAll('.mix-foot button')[1].trigger('click')   // 全用原文
  // 四槽都有原文 → 全回退
  expect(w.text()).toContain('写回 4/4 段')
  await w.get('.mix-foot .btn-primary').trigger('click')
  expect(w.emitted('adopt')[0][0]).toBe('O1\n\nO2\n\nO3\n\nO4')
})

it('单候选不给混合入口：没有得比，混排只是噪音', async () => {
  const w = await runWith(await open(), {
    action: 'rewrite',
    candidates: [{ variant: 0, model: 'gpt-4o-mini', text: 'A1\n\nA2', chars: 4, elapsed_ms: 900 }],
    errors: [],
  })
  expect(w.find('.res-bar .link').exists()).toBe(false)
  expect(w.findAll('.cand')).toHaveLength(1)
})

it('重跑一轮就退出混排：旧勾选对新候选毫无意义', async () => {
  const w = await runWith(await open(), TWO)
  await w.get('.res-bar .link').trigger('click')
  expect(w.find('.mix').exists()).toBe(true)
  await runWith(w, TWO)                                    // 再点一次开始
  expect(w.find('.mix').exists()).toBe(false)
})
