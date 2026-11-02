/**
 * 模板的序列化与反序列化。
 *
 * 抽成模块是因为写作台（stores/studio.js）与控制台（ConsoleView.vue）
 * 各有一份入口：各写一份解析，总有一处忘 try/catch 把一个坏模板变成白屏。
 */

/** 库里的 outline_json / checks_json / tags_json 是 JSON 文本，界面要的是数组 */
export function decodeTemplate(t) {
  const arr = (raw) => {
    if (Array.isArray(raw)) return raw
    try {
      const got = JSON.parse(raw || '[]')
      return Array.isArray(got) ? got : []
    } catch {
      return []
    }
  }
  return { ...t, outline: arr(t.outline_json), checks: arr(t.checks_json), tags: arr(t.tags_json) }
}

/** 骨架 → 一行一节「标题 | 要点 | 字数」，控制台编辑用 */
export function outlineToText(list) {
  return (list || [])
    .map((s) => `${s.heading || ''} | ${(s.prompt || '').replace(/\s*\n\s*/g, ' ')} | ${s.target_words || ''}`)
    .join('\n')
}

/**
 * 纯文本 → 骨架。空行与无标题的行都跳过：
 * 没有标题的节进了 S0，模型会照着写一段没有主题的话。
 *
 * 要点里允许再出现竖线，所以只把最后一段当字数，中间几段并回要点。
 */
export function textToOutline(text) {
  const out = []
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const parts = line.split('|')
    const heading = (parts.shift() || '').trim()
    if (!heading) continue
    const tail = (parts[parts.length - 1] || '').trim()
    const words = parts.length >= 2 ? Number(tail) : NaN
    const promptParts = Number.isFinite(words) && tail ? parts.slice(0, -1) : parts
    out.push({
      heading,
      prompt: promptParts.join('|').trim(),
      target_words: Number.isFinite(words) && words > 0 ? Math.round(words) : 0,
    })
  }
  return out
}
