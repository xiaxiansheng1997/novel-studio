/**
 * 文本差异比对：只用来给「历史」页显示两版正文差在哪，不参与任何写库判断。
 *
 * 不引依赖是有意的：项目现有 deps 里没有 diff 库，为一个人眼看的对比视图
 * 拖进 30KB 不值当；而按行做 LCS 手写四十来行就够，作者看得懂「改了哪几段」即可，
 * 不需要字符级的精确。
 */

// LCS 是 O(n·m) 的表。两版正文行数乘积超过这个数就不硬算了，
// 直接按「整段替换」呈现——宁可差异粗一点，也不能让长章切到历史页把浏览器卡死。
const MAX_CELLS = 900000

function splitLines(text) {
  const s = String(text ?? '')
  // 空正文就是空正文，别让它变成「一行空字符串」——那样每次比对都多出一条删除
  if (!s) return []
  const lines = s.split('\n')
  // 结尾空行是排版痕迹，不是内容；留着会让两版看起来「末尾多了一段」
  while (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return lines
}

/** 中段暴力 LCS：输出 [{type: 'same' | 'add' | 'del', text}]，add 是新版独有的行 */
function lcsRows(a, b) {
  const n = a.length
  const m = b.length
  if (!n && !m) return []
  if (n * m > MAX_CELLS) {
    return [
      ...a.map((text) => ({ type: 'del', text })),
      ...b.map((text) => ({ type: 'add', text })),
    ]
  }
  // dp[i][j] = a[i:] 与 b[j:] 的最长公共子序列长度
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    const cur = dp[i]
    const next = dp[i + 1]
    for (let j = m - 1; j >= 0; j--) {
      cur[j] = a[i] === b[j] ? next[j + 1] + 1 : Math.max(next[j], cur[j + 1])
    }
  }
  const rows = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ type: 'del', text: a[i] })
      i++
    } else {
      rows.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < n) rows.push({ type: 'del', text: a[i++] })
  while (j < m) rows.push({ type: 'add', text: b[j++] })
  return rows
}

/**
 * 比对两版正文。
 *
 * @param before 当前正文（左栏：现在库里这份）
 * @param after 历史版本正文（右栏：准备回滚过去的那份）
 * @returns 行序列 + 增减行数，del 表示「只有当前版有」，add 表示「只有那一版有」
 */
export function diffText(before, after) {
  const a = splitLines(before)
  const b = splitLines(after)
  // 先掐掉公共前后缀：改稿通常只动中间几段，两头一致的部分没必要进 LCS
  let head = 0
  while (head < a.length && head < b.length && a[head] === b[head]) head++
  let ia = a.length - 1
  let ib = b.length - 1
  while (ia >= head && ib >= head && a[ia] === b[ib]) {
    ia--
    ib--
  }
  const rows = []
  for (let k = 0; k < head; k++) rows.push({ type: 'same', text: a[k] })
  rows.push(...lcsRows(a.slice(head, ia + 1), b.slice(head, ib + 1)))
  for (let k = ib + 1; k < b.length; k++) rows.push({ type: 'same', text: b[k] })
  return {
    rows,
    added: rows.filter((r) => r.type === 'add').length,
    removed: rows.filter((r) => r.type === 'del').length,
  }
}

/** 左栏（当前正文）：公共行 + 只有当前版有的行 */
export function currentRows(rows) {
  return rows.filter((r) => r.type !== 'add')
}

/** 右栏（那一版）：公共行 + 只有那一版有的行 */
export function versionRows(rows) {
  return rows.filter((r) => r.type !== 'del')
}
