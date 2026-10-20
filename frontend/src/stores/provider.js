import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getApiKey } from '../utils/storage'

export const useProviderStore = defineStore('provider', () => {
  const models = ref([])
  const status = ref([])
  const selectedModel = ref(localStorage.getItem('w2a_model') || '')

  async function fetchModels() {
    // 配置了 W2A_API_KEY 时需要带上令牌，否则模型列表会 401
    const apiKey = getApiKey()
    const headers = apiKey ? { Authorization: 'Bearer ' + apiKey } : {}
    const r = await fetch('/v1/models', { headers })
    if (r.ok) {
      const j = await r.json()
      models.value = j.data || []
      if (!selectedModel.value && models.value.length) {
        selectedModel.value = models.value[0].id
      }
    }
  }

  async function fetchStatus() {
    const r = await fetch('/admin/status')
    if (r.ok) {
      const j = await r.json()
      status.value = j.providers || []
    }
  }

  function setModel(m) {
    selectedModel.value = m
    localStorage.setItem('w2a_model', m)
  }

  return { models, status, selectedModel, fetchModels, fetchStatus, setModel }
})
