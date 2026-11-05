/**
 * 队列「几路在跑」的读取口径（src/utils/queue.js）。
 *
 * 后端从并行改造起同时给 current 与 running：current 只是最早开跑的那一路。
 * 这个口子最容易出的两种错都在下面钉住——只认 running 会让老响应显示成空闲，
 * 只认 current 会把三路显示成一路（用户以为另两路没干活）。
 */
import { expect, it } from 'vitest'

import { laneCount, queueRunningTitle, runningJobs } from '../src/utils/queue'

const job = (id, title = '') => ({ job_id: id, task_type: 'craft', title, elapsed_ms: 1000 })

it('running 优先：并发批次有几路就报几路', () => {
  const q = { current: job('j1'), running: [job('j1'), job('j2'), job('j3')] }
  expect(runningJobs(q).map((j) => j.job_id)).toEqual(['j1', 'j2', 'j3'])
  expect(laneCount(q)).toBe(3)
})

it('老响应只有 current：退回一路，不能显示成空闲', () => {
  const q = { current: job('j1', '第1章') }
  expect(laneCount(q)).toBe(1)
  expect(queueRunningTitle(q)).toBe('正在跑：第1章')
})

it('running 是空数组时才算排空：current 为 null 的老响应同样落到空', () => {
  expect(runningJobs({ current: null, running: [] })).toEqual([])
  expect(laneCount({ current: null, running: [] })).toBe(0)
  expect(queueRunningTitle({ current: null, running: [] })).toBe('')
  expect(laneCount(null)).toBe(0)
})

it('并行时把路数放在最前面：ETA 突然变短得有解释', () => {
  const q = { running: [job('j1', '第2章'), job('j2', '第3章')] }
  expect(queueRunningTitle(q)).toBe('并行 2 路：第2章 等 2 个')
})

it('没节标题就落到任务类型：拆解这类任务本来就没有 title', () => {
  const q = { running: [{ job_id: 'j1', task_type: 'craft', elapsed_ms: 100 }] }
  expect(queueRunningTitle(q)).toBe('正在跑：craft')
})
