/**
 * 队列状态（/studio/queue/status）的读取口径。
 *
 * 后端从 P0 起把并行批次整个摊在 running 里，current 只是「代表任务」，
 * 留着是为了不砸老前端。这一层负责把「有几个在跑」这件事算对：
 * 只认 running 会让老响应显示成空闲，只认 current 会把三路显示成一路，
 * 所以两种都吃、以 running 为准。
 */

/** 正在跑的任务列表；旧响应没有 running 时退回单个 current。 */
export function runningJobs(queue) {
  if (!queue) return []
  const list = Array.isArray(queue.running) ? queue.running : []
  if (list.length) return list
  return queue.current ? [queue.current] : []
}

/** 同时跑几路：1 就是串行，界面上不该出现「并行」字样。 */
export function laneCount(queue) {
  return runningJobs(queue).length
}

/**
 * 一行状态标题：串行说「正在跑」，并行把路数摆在最前面。
 *
 * 路数放前面是有意的——用户先看 ETA，得让他一眼明白 ETA 为什么突然变短了。
 */
export function queueRunningTitle(queue) {
  const jobs = runningJobs(queue)
  if (!jobs.length) return ''
  const name = jobs[0].title || jobs[0].task_type || '任务'
  if (jobs.length === 1) return `正在跑：${name}`
  return `并行 ${jobs.length} 路：${name} 等 ${jobs.length} 个`
}
