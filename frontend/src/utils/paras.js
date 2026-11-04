/**
 * 正文分段（带原文偏移）：编辑器衬底渲染与「当前段」追踪共用的一份切法。
 *
 * 为什么连分隔符也原样留着：衬底要逐字复现 textarea 的换行排版，
 * 最好的办法是**一个字符都不丢**——把 \n 从段与段之间抽掉、再靠 margin 去补
 * 空行的高度，等于把「和 textarea 对齐」交给两坨 CSS 互相追赶，迟早对不上。
 * 于是切完的数组里正文与分隔符交替出现，text 按序拼回去 === 原文。
 *
 * 分隔符规则与加工弹窗一致：空行或单换行都算分段（\n\s*\n 优先吃掉整个空行，
 * 剩下的单 \n 也成界）；中文稿里两种都会出现。
 */

const SEP_RE = /\n\s*\n|\n/g

/**
 * 返回 [{ text, sep, start, end }]。start/end 是这段在全文里的偏移（半开区间），
 * 与 textarea 的 selectionStart 同一坐标系——「当前段」全靠它俩对表。
 */
export function splitWithOffsets(text) {
  const s = text || ''
  const parts = []
  const re = new RegExp(SEP_RE.source, 'g')
  let last = 0
  let m
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) {
      parts.push({ text: s.slice(last, m.index), sep: false, start: last, end: m.index })
    }
    parts.push({ text: m[0], sep: true, start: m.index, end: m.index + m[0].length })
    last = m.index + m[0].length
  }
  if (last < s.length) {
    parts.push({ text: s.slice(last), sep: false, start: last, end: s.length })
  }
  return parts
}

/**
 * 光标落在哪个正文段上，返回该段在 parts 里的下标；没有则 -1。
 * 界于两段之间的分隔符不算「站在」任何一段上，但光标常停在段尾（还没按下回车前），
 * 所以精确命中失败时退回「左边最近的正文段」——段尾打字时淡化不该跳到下一段。
 */
export function activePartAt(parts, pos) {
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i]
    if (!p.sep && p.start <= pos && pos < p.end) return i
  }
  for (let i = parts.length - 1; i >= 0; i--) {
    if (!parts[i].sep && parts[i].start <= pos) return i
  }
  return -1
}
