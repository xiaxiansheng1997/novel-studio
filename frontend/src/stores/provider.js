import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getApiKey } from '../utils/storage'

// 模式开关的默认值：模型名后缀 / 模型自带特性都会影响初始状态
function deriveModes(modelId, model) {
  const id = String(modelId || '')
  const feats = (model && model.features) || {}
  const expert = /-expert$/.test(id)
  return {
    thinking: Boolean(feats.thinking) || expert || /-think$/.test(id) || id === 'deepseek-reasoner',
    search: Boolean(feats.search) || expert || /-search$/.test(id),
    expert,
  }
}

export const useProviderStore = defineStore('provider', () => {
  // 统一后的模型条目：{ id, label, provider, display, features }
  const models = ref([])
  const providers = ref([])
  const status = ref([])
  const selectedModel = ref(localStorage.getItem('w2a_model') || '')
  const modes = ref({ thinking: false, search: false, expert: false })

  const currentModel = computed(() => models.value.find(m => m.id === selectedModel.value) || null)
  const currentProvider = computed(() => {
    const m = currentModel.value
    if (!m) return null
    return providers.value.find(p => p.key === m.provider) || null
  })
  const modelGroups = computed(() => {
    const groups = []
    for (const p of providers.value) {
      const list = models.value.filter(m => m.provider === p.key)
      if (list.length) groups.push({ key: p.key, name: p.display_name || p.key, models: list })
    }
    // 没有能力信息时（老后端）也要能下拉
    if (!groups.length && models.value.length) {
      groups.push({ key: 'all', name: '全部模型', models: models.value })
    }
    return groups
  })
  const supports = computed(() => {
    const feats = (currentProvider.value && currentProvider.value.features) || []
    return {
      thinking: feats.includes('thinking'),
      search: feats.includes('search'),
      models: feats.includes('models'),
    }
  })

  function applyModelModes() {
    const m = currentModel.value
    modes.value = deriveModes(selectedModel.value, m)
    saveModes()
  }

  function saveModes() {
    localStorage.setItem('w2a_modes', JSON.stringify(modes.value))
  }

  function loadModes() {
    try {
      const saved = JSON.parse(localStorage.getItem('w2a_modes') || 'null')
      if (saved && typeof saved === 'object') {
        modes.value = {
          thinking: Boolean(saved.thinking),
          search: Boolean(saved.search),
          expert: Boolean(saved.expert),
        }
        return true
      }
    } catch (_) {
      // 本地值坏了就按模型推导
    }
    return false
  }

  function flattenModels(list) {
    const out = []
    for (const p of list) {
      for (const m of p.models || []) {
        out.push({
          id: m.id,
          label: m.label || m.id,
          provider: p.key,
          display: p.display_name || p.key,
          features: m.features || {},
        })
      }
    }
    return out
  }

  async function fetchModels() {
    // 配置了 W2A_API_KEY 时需要带上令牌，否则模型列表会 401
    const apiKey = getApiKey()
    const headers = apiKey ? { Authorization: 'Bearer ' + apiKey } : {}
    let loaded = false
    try {
      const r = await fetch('/v1/capabilities', { headers })
      if (r.ok) {
        const j = await r.json()
        providers.value = j.providers || []
        models.value = flattenModels(providers.value)
        loaded = models.value.length > 0
      }
    } catch (_) {
      loaded = false
    }
    if (!loaded) {
      // 老后端兜底：只有 /v1/models
      const r = await fetch('/v1/models', { headers })
      if (r.ok) {
        const j = await r.json()
        models.value = (j.data || []).map(m => ({
          id: m.id,
          label: m.id,
          provider: '',
          display: m.owned_by || '',
          features: {},
        }))
      }
    }
    if (!selectedModel.value || !models.value.some(m => m.id === selectedModel.value)) {
      selectedModel.value = models.value.length ? models.value[0].id : ''
    }
    if (!loadModes()) applyModelModes()
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
    applyModelModes()
  }

  function setMode(key, value) {
    if (key === 'expert') {
      modes.value.expert = value
      if (value) {
        modes.value.thinking = true
        modes.value.search = true
      }
    } else {
      modes.value[key] = value
      if (!value) modes.value.expert = false
    }
    saveModes()
  }

  function modePayload() {
    const m = modes.value
    return {
      thinking: m.thinking,
      search: m.search,
      mode: m.expert ? 'expert' : null,
    }
  }

  return {
    models,
    providers,
    status,
    selectedModel,
    modes,
    currentModel,
    currentProvider,
    modelGroups,
    supports,
    fetchModels,
    fetchStatus,
    setModel,
    setMode,
    modePayload,
  }
})
