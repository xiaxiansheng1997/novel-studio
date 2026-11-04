/**
 * BatchEstimateDialog：「开跑前看一眼」这层弹层自己的行为。
 * 超限判定和忽略预算勾选最要紧——坏了要么闸门形同虚设，要么把清醒的决定拦死。
 * store 整个换成替身：这里不起后端，也不测预估口径（那在 pytest 里）。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const estFixture = () => ({
  sections: 4,
  per_section_prompt_chars: 10000,
  per_section_output_chars: 1500,
  prompt_chars_total: 40000,
  output_chars_total: 6000,
  est_prompt_tokens: 20000,
  est_output_tokens: 3000,
  basis: 'runs_median',
  basis_note: '按本书最近 3 次成功生成的字数中位数',
  priced: true,
  est_cost: 3.2,
  budget_limit: 3.0,
  over_budget: true,
  model: 'gpt-4o-mini',
})

const store = vi.hoisted(() => ({
  work: { title: '预算作品' },
  estimatePipeline: vi.fn(),
  getBudget: vi.fn(async () => 3.0),
  setBudget: vi.fn(async () => true),
}))

vi.mock('../src/stores/studio', () => ({ useStudioStore: () => store }))

// eslint-disable-next-line import/first
import BatchEstimateDialog from '../src/components/BatchEstimateDialog.vue'

beforeEach(() => {
  store.estimatePipeline.mockReset().mockResolvedValue(estFixture())
  store.setBudget.mockClear().mockResolvedValue(true)
})

/** 组件靠 watch(open)  false→true 触发拉取：直接以 open:true 挂载不会发请求 */
async function openDialog(mode = 'pipeline', extraProps = {}) {
  const w = mount(BatchEstimateDialog, { props: { open: false, mode, ...extraProps } })
  await w.setProps({ open: true })
  await flushPromises()
  return w
}

it('不开着就不渲染弹层', () => {
  const w = mount(BatchEstimateDialog, { props: { open: false, mode: 'pipeline' } })
  expect(w.find('.dlg').exists()).toBe(false)
  expect(store.estimatePipeline).not.toHaveBeenCalled()
})

it('打开就拉预估：四格数字与依据说明都在', async () => {
  const w = await openDialog()
  expect(store.estimatePipeline).toHaveBeenCalledTimes(1)
  expect(w.find('.nums').exists()).toBe(true)
  expect(w.text()).toContain('6,000')
  expect(w.text()).toContain('≈ 3.2')
  expect(w.text()).toContain('按本书最近 3 次成功生成的字数中位数')
})

it('超预算：没勾忽略时 confirm 不透传 ignore_budget，勾了才透传', async () => {
  const w = await openDialog()
  expect(w.find('.err').exists()).toBe(true)
  await w.get('.btn-primary').trigger('click')
  expect(w.emitted('confirm')[0][0]).toEqual({ ignoreBudget: false })
  await w.get('.check input').setValue(true)
  await w.get('.btn-primary').trigger('click')
  expect(w.emitted('confirm')[1][0]).toEqual({ ignoreBudget: true })
})

it('反代通道：显示「不计钱」，不摆预算拦截那套', async () => {
  store.estimatePipeline.mockResolvedValue(
    { ...estFixture(), priced: false, est_cost: null, over_budget: false },
  )
  const w = await openDialog()
  expect(w.text()).toContain('不计钱')
  expect(w.find('.check input').exists()).toBe(false)
})

it('存上限后重算一次：不能让人按旧结论点确认', async () => {
  const w = await openDialog()
  await w.get('.budget input').setValue('8')
  await w.get('.budget .btn').trigger('click')
  await flushPromises()
  expect(store.setBudget).toHaveBeenCalledWith(8)
  expect(store.estimatePipeline).toHaveBeenCalledTimes(2)
})

it('预估吃调用方传来的参数：不靠两边碰巧一样的默认值（审查 M1）', async () => {
  const w = await openDialog('pipeline', { params: { limit: 2, only_empty: false } })
  expect(store.estimatePipeline).toHaveBeenCalledWith({ limit: 2, only_empty: false })
  expect(w).toBeTruthy()
})

it('预算框清空不存：NaN 落到后端是 0=不设限，静默解闸门（审查 m4）', async () => {
  const w = await openDialog()
  await w.get('.budget input').setValue('')
  await w.get('.budget .btn').trigger('click')
  await flushPromises()
  expect(store.setBudget).not.toHaveBeenCalled()
  expect(w.text()).toContain('要给个数字')
  // 显式输 0 才是解除上限，这条路得通
  await w.get('.budget input').setValue('0')
  await w.get('.budget .btn').trigger('click')
  await flushPromises()
  expect(store.setBudget).toHaveBeenCalledWith(0)
})
