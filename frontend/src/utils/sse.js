/**
 * SSE stream parser for fetch responses.
 * Yields parsed JSON objects from `data:` lines.
 */
export async function* parseSSE(response) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })

    let idx
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 1)

      if (!line.startsWith('data:')) continue
      const data = line.slice(5).trim()
      if (data === '[DONE]') return
      try {
        yield JSON.parse(data)
      } catch (_) {
        // skip malformed JSON
      }
    }
  }
}

/**
 * Send a streaming chat completion request.
 * Returns an async generator yielding { content, reasoning_content, finish_reason }.
 */
export async function* streamChat({ model, messages, signal, apiKey, conversationId }) {
  const headers = { 'Content-Type': 'application/json' }
  if (apiKey) headers['Authorization'] = `Bearer ${apiKey}`

  const body = { model, messages, stream: true }
  if (conversationId) body.conversation_id = conversationId

  const resp = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers,
    signal,
    body: JSON.stringify(body),
  })

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(err?.error?.message || `HTTP ${resp.status}`)
  }

  for await (const chunk of parseSSE(resp)) {
    if (chunk.error) throw new Error(chunk.error.message)
    const delta = chunk.choices?.[0]?.delta || {}
    const finish = chunk.choices?.[0]?.finish_reason
    if (delta.content || delta.reasoning_content || finish) {
      yield { content: delta.content, reasoning_content: delta.reasoning_content, finish_reason: finish }
    }
  }
}
