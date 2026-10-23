import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiFetch } from '../utils/api'

export const useChatStore = defineStore('chat', () => {
  const conversations = ref([])
  const activeId = ref(null)
  const messages = ref([])
  const streaming = ref(false)
  const abortController = ref(null)

  const activeConversation = computed(() =>
    conversations.value.find(c => c.id === activeId.value)
  )

  async function fetchConversations() {
    const r = await apiFetch('/v1/conversations')
    if (r.ok) conversations.value = await r.json()
  }

  async function createConversation(model) {
    const r = await apiFetch('/v1/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, title: '新对话' }),
    })
    const conv = await r.json()
    conversations.value.unshift(conv)
    activeId.value = conv.id
    messages.value = []
    return conv
  }

  async function selectConversation(id) {
    activeId.value = id
    const r = await apiFetch(`/v1/conversations/${id}/messages`)
    if (r.ok) messages.value = await r.json()
    else messages.value = []
  }

  async function deleteConversation(id) {
    await apiFetch(`/v1/conversations/${id}`, { method: 'DELETE' })
    conversations.value = conversations.value.filter(c => c.id !== id)
    if (activeId.value === id) {
      activeId.value = null
      messages.value = []
    }
  }

  async function renameConversation(id, title) {
    await apiFetch(`/v1/conversations/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    })
    const conv = conversations.value.find(c => c.id === id)
    if (conv) conv.title = title
  }

  // 回退会话：带 messageId = 从这条（含）删到末尾（编辑重发）；不带 = 只删末尾的助手回答（重新生成）
  async function rewindConversation(id, messageId) {
    if (!id) return
    await apiFetch(`/v1/conversations/${id}/rewind`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(messageId ? { message_id: messageId } : {}),
    })
  }

  // 消息全文搜索（侧栏用）：返回 [{conversation_id, title, snippet, ...}]
  async function searchMessages(q) {
    if (!q || q.trim().length < 2) return []
    const r = await apiFetch(`/v1/search?q=${encodeURIComponent(q.trim())}`)
    if (!r.ok) return []
    const j = await r.json()
    return j.items || []
  }

  async function exportConversation(id) {
    const r = await apiFetch(`/v1/conversations/${id}/export`, { method: 'POST' })
    return r.ok ? r.json() : null
  }

  function stopStreaming() {
    if (abortController.value) {
      abortController.value.abort()
      abortController.value = null
    }
    streaming.value = false
  }

  return {
    conversations, activeId, messages, streaming, abortController,
    activeConversation,
    fetchConversations, createConversation, selectConversation,
    deleteConversation, renameConversation, stopStreaming,
    rewindConversation, searchMessages, exportConversation,
  }
})
