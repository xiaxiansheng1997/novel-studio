/**
 * 章内查找的字面匹配：与 StudioView 里的查找条共用一条口径。
 * 抽成纯函数是为了能被 Vitest 直接测——命中位置算错一格，圈选就偏一整段。
 */

/**
 * 在 text 里找 needle 的所有出现（忽略大小写），返回 [[start, end], ...]。
 * 按 start 升序；needle 为空不给命中（空串匹配每个位置没有意义）。
 */
export function collectHits(text, needle) {
  const hay = (text || '').toLowerCase()
  const nd = (needle || '').toLowerCase()
  const hits = []
  if (!nd) return hits
  for (let at = hay.indexOf(nd); at >= 0; at = hay.indexOf(nd, at + nd.length)) {
    hits.push([at, at + nd.length])
  }
  return hits
}

/** 环形步进：i 加减 delta 后落在 [0, n) 内；n<=0 时给 0（没有命中不跳） */
export function stepIndex(i, delta, n) {
  if (n <= 0) return 0
  return ((i + delta) % n + n) % n
}
