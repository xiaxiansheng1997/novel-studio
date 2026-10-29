import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { apiFetch } from '../utils/api'

/** 写作台的模型选择单独存一份 key：和聊天页的 w2a_model 互不干扰 */
const STUDIO_MODEL_KEY = 'w2a_studio_model'

/** 后端错误体是 FastAPI 的 `{detail: "..."}`，删除被备份拦住那句就在里面 */
async function errDetail(r, fallback) {
  try {
    const j = await r.json()
    return typeof j.detail === 'string' ? j.detail : fallback
  } catch {
    return fallback
  }
}

/**
 * 写作台状态：作品 → 节点树 → 当前节点 → 生成。
 *
 * 设计取舍：所有写操作都走服务端（没有 localStorage 副本），
 * 避免多标签页/刷新后出现两套真相。正文自动保存用 debounce，见 StudioView。
 */
export const useStudioStore = defineStore('studio', () => {
  const works = ref([])
  const work = ref(null)
  const tree = ref([])
  const node = ref(null)
  const stats = ref(null)
  const templates = ref([])

  const sources = ref([])
  const facts = ref([])
  const terms = ref([])
  const issues = ref([])
  // 公众号那一侧：文风画像是全局的，发布台账是按作品的
  const voice = ref({ samples: [], profile: '', chars: 0 })
  const settings = ref({})
  const publications = ref([])
  const pubSummary = ref([])
  // 选题池是全局的；写作统计只有一份（按天净增字数，从版本表现算）
  const ideas = ref([])
  const dailyWrites = ref({ days: [], today: 0, saves_today: 0, streak: 0, total: 0 })

  const loading = ref(false)
  const saving = ref(false)
  const error = ref('')

  // 生成态：genText 与 node.content 分开，用户点「采用」才写回
  const generating = ref(false)
  const genText = ref('')
  const genMeta = ref(null)
  const genError = ref('')

  // 写作台用哪个模型。空串 = 请求里不带 model，交给后端 pick_model 落到它的默认主力；
  // 所以「自动」这一档永远有效，即使配置里把那家关掉了也不会报错。
  const genModel = ref(localStorage.getItem(STUDIO_MODEL_KEY) || '')
  const modelGroups = ref([])

  const activeWorkId = computed(() => work.value?.id || '')
  const activeNodeId = computed(() => node.value?.id || '')
  const template = computed(() =>
    templates.value.find((t) => t.id === work.value?.template_id) || null
  )

  const allGenModels = computed(() => modelGroups.value.flatMap((g) => g.models))

  /** 下拉框上显示的名字；真正用了哪个模型以服务端返回的 meta 为准。 */
  const genModelLabel = computed(() => {
    if (!genModel.value) return '自动'
    return allGenModels.value.find((m) => m.id === genModel.value)?.label || genModel.value
  })

  function setGenModel(id) {
    genModel.value = id
    if (id) localStorage.setItem(STUDIO_MODEL_KEY, id)
    else localStorage.removeItem(STUDIO_MODEL_KEY)
  }

  /**
   * 模型目录来自 /v1/capabilities（三家 + 模式别名）。
   * 别名（deepseek-chat-think 这类）也列出来：写作接口没有思考/联网开关，
   * 想要这些档位只能靠模型名后缀表达。
   */
  async function loadGenModels() {
    const r = await apiFetch('/v1/capabilities')
    if (r.ok) {
      const j = await r.json()
      const groups = []
      for (const p of j.providers || []) {
        const models = (p.models || []).map((m) => ({ id: m.id, label: m.label || m.id }))
        for (const alias of p.aliases || []) models.push({ id: alias, label: alias })
        if (models.length) groups.push({ key: p.key, name: p.display_name || p.key, models })
      }
      modelGroups.value = groups
    } else {
      // 老后端没有能力矩阵，退回 /v1/models：只有 id，按 owned_by 分组
      const fb = await apiFetch('/v1/models')
      if (!fb.ok) return
      const byOwner = new Map()
      for (const m of (await fb.json()).data || []) {
        const owner = m.owned_by || '其他'
        if (!byOwner.has(owner)) byOwner.set(owner, [])
        byOwner.get(owner).push({ id: m.id, label: m.id })
      }
      modelGroups.value = [...byOwner].map(([key, models]) => ({ key, name: key, models }))
    }
    // 存过的模型可能已经不在配置里（config.yaml 关掉了某一家）：清回自动，
    // 否则下拉框显示的是 id、发出去的却是个无效值
    if (genModel.value && !allGenModels.value.some((m) => m.id === genModel.value)) {
      setGenModel('')
    }
  }

  async function loadTemplates(kind, subtype) {
    const q = new URLSearchParams()
    if (kind) q.set('kind', kind)
    if (subtype) q.set('subtype', subtype)
    const r = await apiFetch('/studio/templates?' + q.toString())
    if (r.ok) templates.value = await r.json()
  }

  async function fetchWorks(kind = '') {
    const q = kind ? `?kind=${kind}` : ''
    const r = await apiFetch('/studio/works' + q)
    if (r.ok) works.value = await r.json()
  }

  /** 归档 / 恢复：只改 works.status，正文、版本、索引一个都不动 */
  async function setWorkStatus(id, status) {
    const r = await apiFetch(`/studio/works/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, status === 'archived' ? '归档失败' : '恢复失败')
      return false
    }
    const w = await r.json()
    const i = works.value.findIndex((x) => x.id === w.id)
    if (i >= 0) works.value[i] = { ...works.value[i], ...w }
    if (work.value?.id === w.id) work.value = w
    return true
  }

  /** 全库搜索：标题/主旨 + 已建索引的正文与素材；结果由调用方自己持有 */
  async function globalSearch(q) {
    const text = (q || '').trim()
    if (!text) return { works: [], hits: [] }
    const r = await apiFetch('/studio/search?' + new URLSearchParams({ q: text, limit: '20' }))
    if (!r.ok) {
      error.value = await errDetail(r, '搜索失败')
      return { works: [], hits: [] }
    }
    return await r.json()
  }

  // ---- 选题池与写作统计 ----

  async function loadIdeas() {
    const r = await apiFetch('/studio/ideas')
    if (r.ok) ideas.value = await r.json()
  }

  async function addIdea(text) {
    const body = (text || '').trim()
    if (!body) return null
    const r = await apiFetch('/studio/ideas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: body }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '记下失败')
      return null
    }
    const idea = await r.json()
    ideas.value.unshift(idea)
    return idea
  }

  /** 想法变成作品：记下 work_id 后从待写列表里收走 */
  async function useIdea(id, workId) {
    const r = await apiFetch(`/studio/ideas/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'used', work_id: workId }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '标记失败')
      return false
    }
    ideas.value = ideas.value.filter((x) => x.id !== id)
    return true
  }

  async function deleteIdea(id) {
    const r = await apiFetch(`/studio/ideas/${id}`, { method: 'DELETE' })
    if (!r.ok) {
      error.value = await errDetail(r, '删除失败')
      return false
    }
    ideas.value = ideas.value.filter((x) => x.id !== id)
    return true
  }

  async function loadDailyWrites(days = 30) {
    const r = await apiFetch(`/studio/stats/daily?days=${days}`)
    if (r.ok) dailyWrites.value = await r.json()
  }

  async function createWork(payload) {
    const r = await apiFetch('/studio/works', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) throw new Error(await r.text())
    const w = await r.json()
    works.value.unshift(w)
    await selectWork(w.id)
    return w
  }

  /**
   * 改作品本身的信息（标题、主旨）。
   * 建完作品才发现标题想换、主旨没写清楚是常事，以前只有创建那一次能填死，
   * 之后界面上一个入口都没有 —— 而 S2 里那句主旨是每轮生成都看得见的背景。
   */
  async function updateWork(patch) {
    if (!work.value) return false
    error.value = ''
    const r = await apiFetch(`/studio/works/${work.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '更新失败')
      return false
    }
    const w = await r.json()
    work.value = w
    const i = works.value.findIndex((x) => x.id === w.id)
    if (i >= 0) works.value[i] = { ...works.value[i], ...w }
    return true
  }

  /** 删作品连同节点树；当前打开的就是它时清空右侧状态 */
  async function deleteWork(id) {
    const r = await apiFetch(`/studio/works/${id}`, { method: 'DELETE' })
    if (!r.ok) {
      // 删之前服务端会先拷一份整库副本，备份没写成就是 500「已拒绝删除」：
      // 这种时候书还在书架上，不把原因说出来只会被当成按钮没反应
      error.value = await errDetail(r, '删除失败')
      return false
    }
    works.value = works.value.filter((w) => w.id !== id)
    if (work.value?.id === id) {
      work.value = null
      tree.value = []
      node.value = null
      stats.value = null
    }
    return true
  }

  async function selectWork(id) {
    loading.value = true
    error.value = ''
    try {
      const [wr, tr, sr, st] = await Promise.all([
        apiFetch(`/studio/works/${id}`),
        apiFetch(`/studio/works/${id}/tree`),
        apiFetch(`/studio/works/${id}/sources`),
        apiFetch(`/studio/works/${id}/stats`),
      ])
      work.value = await wr.json()
      tree.value = (await tr.json()) || []
      sources.value = (await sr.json()) || []
      stats.value = await st.json()
      node.value = null
      genText.value = ''
      genMeta.value = null
      genError.value = ''
      facts.value = []
      terms.value = []
      issues.value = []
      graphData.value = null
      graphNote.value = ''
      issueSummary.value = null
      queue.value = null
      versions.value = []
      // 右侧面板的数据按需再拉，避免切换作品时打四个请求
    } finally {
      loading.value = false
    }
  }

  async function selectNode(id) {
    error.value = ''
    const r = await apiFetch(`/studio/nodes/${id}`)
    if (!r.ok) {
      error.value = '节点加载失败'
      return
    }
    node.value = await r.json()
    genText.value = ''
    genMeta.value = null
    genError.value = ''
    // 版本跟节点走：先清空再拉，否则标签上的计数会停在上一节那个数字
    versions.value = []
    await loadVersions()
    // 告警跟节点走
    if (work.value) {
      const ir = await apiFetch(`/studio/works/${work.value.id}/issues?node_id=${id}`)
      if (ir.ok) issues.value = await ir.json()
    }
  }

  /**
   * 写回一节。返回是否真的落库——自动保存靠这个返回值决定显示「已保存」还是
   * 「保存失败」。以前失败就地吞掉，界面照样写「已保存」，关掉页面那段字就没了。
   */
  async function saveNode(patch, { snapshot = false, source = '' } = {}) {
    if (!node.value) return false
    saving.value = true
    // 只有「采用 AI 结果」才带来源：历史页要分得清哪一版是模型写的，
    // 手改那一侧不传，由后端落成 human
    const body = { ...patch, snapshot }
    if (snapshot && source) body.snapshot_source = source
    try {
      const r = await apiFetch(`/studio/nodes/${node.value.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) {
        error.value = await errDetail(r, '保存失败')
        return false
      }
      const saved = await r.json()
      node.value = saved
      // 同步树上的字数与标题，避免侧栏显示旧值
      const t = tree.value.find((n) => n.id === saved.id)
      if (t) {
        t.title = saved.title
        t.word_count = saved.word_count
        t.status = saved.status
      }
      // 只有留了版本的写入才需要重拉：自动保存那点调用省下来给「历史」页换内容
      if (snapshot) await loadVersions()
      error.value = ''
      return true
    } catch (e) {
      // fetch 自己抛（断网、服务被关）不会走上面的 !r.ok 分支
      error.value = `保存失败：${String(e?.message || e)}`
      return false
    } finally {
      saving.value = false
    }
  }

  async function createNode(payload) {
    if (!work.value) return null
    const r = await apiFetch(`/studio/works/${work.value.id}/nodes`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      error.value = await r.text()
      return null
    }
    const n = await r.json()
    tree.value = await (await apiFetch(`/studio/works/${work.value.id}/tree`)).json()
    await selectNode(n.id)
    return n
  }

  /**
   * 逐节导入旧稿。
   *
   * 每节都走 createNode 那条接口，但不在循环里 select：八十章会触发八十次
   * 「选中 + 重拉整棵树」。最后统一重拉一次树、刷一次字数、重建一次索引就够了。
   * 导入的正文不计入「今天写的字」——create_node 本来就不记，别在别处补。
   */
  async function importNodes(sections, { kind = 'chapter', parentId = null } = {}, onProgress = null) {
    if (!work.value) return { created: 0, failed: 0, firstId: '' }
    let created = 0
    let failed = 0
    let firstId = ''
    for (const [i, s] of sections.entries()) {
      const r = await apiFetch(`/studio/works/${work.value.id}/nodes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind,
          title: s.title,
          content: s.content,
          parent_id: parentId || null,
        }),
      })
      if (r.ok) {
        const row = await r.json()
        if (!firstId) firstId = row.id
        created += 1
      } else {
        failed += 1
      }
      onProgress?.(i + 1, sections.length)
    }
    tree.value = await (await apiFetch(`/studio/works/${work.value.id}/tree`)).json()
    const st = await apiFetch(`/studio/works/${work.value.id}/stats`)
    if (st.ok) stats.value = await st.json()
    // 导进来的正文还没进 FTS：不建索引，刚导入的章搜不到、生成也用不上
    if (created) {
      try {
        await reindex()
      } catch {
        /* 索引失败不该让导入回滚 */
      }
    }
    return { created, failed, firstId }
  }

  /** 删节点连同子树。备份写不成时后端会拒绝删除，这里把原因带到 error 上 */
  async function deleteNode(id) {
    const r = await apiFetch(`/studio/nodes/${id}`, { method: 'DELETE' })
    if (!r.ok) {
      error.value = await errDetail(r, '删除失败')
      return false
    }
    error.value = ''
    tree.value = tree.value.filter((n) => n.id !== id && !n.path.startsWith((tree.value.find((x) => x.id === id)?.path || '∅') + '.'))
    if (node.value?.id === id) node.value = null
    return true
  }

  /**
   * 同层上下移：把这一节和紧邻的前/后兄弟互换。返回接口的响应体
   * （`{moved, ordinal, swapped_with_title, reason}`），「已到边界」不是错误，
   * 由调用方拿 reason 当提示语显示。
   *
   * 树上的顺序来自 path、生成顺序来自 ordinal，两头都换了，所以整棵树只能重拉；
   * 当前节还是原来那一节，重新 select 一次把章号/字数带回来，不然编辑器顶部
   * 显示的还是旧 ordinal。
   */
  async function moveNode(id, direction) {
    const r = await apiFetch(`/studio/nodes/${id}/move`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction }),
    })
    if (!r.ok) {
      error.value = '移动失败'
      return { moved: false, reason: `移动失败（HTTP ${r.status}）` }
    }
    const res = await r.json()
    if (res.moved && work.value) {
      tree.value = await (await apiFetch(`/studio/works/${work.value.id}/tree`)).json()
      if (node.value?.id === id) await selectNode(id)
    }
    return res
  }

  async function loadSources() {
    if (!work.value) return
    const r = await apiFetch(`/studio/works/${work.value.id}/sources`)
    if (r.ok) sources.value = await r.json()
  }

  async function addSource(payload) {
    error.value = ''
    const r = await apiFetch(`/studio/works/${work.value.id}/sources`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      // 旧写法只回 ok，导入失败在界面上就是「点了没反应」
      error.value = await errDetail(r, '导入素材失败')
      return false
    }
    await loadSources()
    return true
  }

  async function loadSource(id) {
    const r = await apiFetch(`/studio/sources/${id}`)
    if (!r.ok) {
      error.value = await errDetail(r, '打不开这条素材')
      return null
    }
    return await r.json()
  }

  /** 只搜素材正文：scope=source，走与生成时同一套 FTS */
  async function searchSources(q) {
    if (!work.value || !q) return []
    const r = await apiFetch(`/studio/works/${work.value.id}/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q, scope: 'source', top_k: 10 }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '检索失败')
      return []
    }
    const data = await r.json()
    return data.hits || []
  }

  /** 从素材抽事实卡：抽完刷一次事实列表，否则面板上还是旧的 */
  async function extractFacts(sourceId, limit = 5) {
    error.value = ''
    const r = await apiFetch(`/studio/sources/${sourceId}/facts/extract`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '抽取事实失败')
      return null
    }
    await loadFacts()
    return await r.json()
  }

  async function deleteSource(id) {
    const r = await apiFetch(`/studio/sources/${id}`, { method: 'DELETE' })
    if (!r.ok) error.value = await errDetail(r, '删除素材失败')
    await loadSources()
  }

  /** 改素材：正文改了后端会重建这一条的索引，回来直接把新行给调用方 */
  async function updateSource(id, payload) {
    error.value = ''
    const r = await apiFetch(`/studio/sources/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '保存素材失败')
      return null
    }
    const row = await r.json()
    await loadSources()
    return row
  }

  async function loadFacts() {
    if (!work.value) return
    const r = await apiFetch(`/studio/works/${work.value.id}/facts`)
    if (r.ok) facts.value = await r.json()
  }

  async function addFact(payload) {
    const r = await apiFetch(`/studio/works/${work.value.id}/facts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (r.ok) await loadFacts()
    return r.ok
  }

  async function deleteFact(id) {
    await apiFetch(`/studio/facts/${id}`, { method: 'DELETE' })
    await loadFacts()
  }

  async function updateFact(id, payload) {
    const r = await apiFetch(`/studio/facts/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '保存事实失败')
      return false
    }
    await loadFacts()
    return true
  }

  async function loadTerms() {
    if (!work.value) return
    const r = await apiFetch(`/studio/works/${work.value.id}/terms`)
    if (r.ok) terms.value = await r.json()
  }

  async function addTerm(payload) {
    const r = await apiFetch(`/studio/works/${work.value.id}/terms`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (r.ok) await loadTerms()
    return r.ok
  }

  async function deleteTerm(id) {
    await apiFetch(`/studio/terms/${id}`, { method: 'DELETE' })
    await loadTerms()
  }

  async function updateTerm(id, payload) {
    const r = await apiFetch(`/studio/terms/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '保存术语失败')
      return false
    }
    await loadTerms()
    return true
  }

  /** 只装配不调用 LLM，用来调 prompt / 看预算 */
  async function previewContext({ task_type, instruction }) {
    if (!work.value) return null
    const q = new URLSearchParams({ work_id: work.value.id })
    if (node.value) q.set('node_id', node.value.id)
    const r = await apiFetch('/studio/context/preview?' + q.toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_type, instruction }),
    })
    return r.ok ? r.json() : null
  }

  /**
   * 流式生成：读 fetch 的 ReadableStream（EventSource 不支持 POST）。
   * 中途出错只记 genError，不抛异常——UI 还要保留已生成的部分。
   * 点「停止」走 abort：连接一断服务端也会收尾（那一次 run 记为 stopped）。
   */
  let genAbort = null
  const genStopped = ref(false)

  async function generate({ task_type, instruction, save = false, model = genModel.value }) {
    if (!node.value || generating.value) return
    generating.value = true
    genStopped.value = false
    genText.value = ''
    genError.value = ''
    genMeta.value = null
    const ctrl = new AbortController()
    genAbort = ctrl
    try {
      const resp = await apiFetch(`/studio/nodes/${node.value.id}/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_type, instruction, save, model }),
        signal: ctrl.signal,
      })
      if (!resp.ok) {
        genError.value = `HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          for (const line of block.split('\n')) {
            if (!line.startsWith('data:')) continue
            const raw = line.slice(5).trim()
            if (!raw || raw === '[DONE]') continue
            let ev
            try {
              ev = JSON.parse(raw)
            } catch {
              continue
            }
            if (ev.event === 'meta') genMeta.value = ev
            else if (ev.event === 'delta') genText.value += ev.text
            else if (ev.event === 'error') genError.value = ev.message || '生成失败'
            else if (ev.event === 'done' && ev.saved) {
              // 服务端已经写了库，前端同步一次避免出现两套正文
              const r = await apiFetch(`/studio/nodes/${node.value.id}`)
              if (r.ok) node.value = await r.json()
              await loadVersions()
            }
          }
        }
      }
    } catch (e) {
      // 主动停止不是错误：已经收到的部分要留着，用户可以接着采用
      if (ctrl.signal.aborted) genStopped.value = true
      else genError.value = String(e?.message || e)
    } finally {
      generating.value = false
      if (genAbort === ctrl) genAbort = null
    }
  }

  /** 停止生成：断开这条 SSE，服务端那一侧随之收尾 */
  function stopGenerate() {
    if (genAbort) genAbort.abort()
  }

  /** 把生成的文本写回节点（用户在预览条点「采用」），来源按任务给 */
  async function adoptGenerated({ append = false, source = 'ai_draft', text = '' } = {}) {
    const body = text || genText.value
    if (!node.value || !body) return false
    const next = append
      ? (node.value.content ? node.value.content.replace(/\s+$/, '') + '\n\n' + body : body)
      : body
    // 写库失败就留着预览：清掉的话这段生成结果连重试的机会都没有
    if (!(await saveNode({ content: next }, { snapshot: true, source }))) return false
    genText.value = ''
    genMeta.value = null
    return true
  }

  function discardGenerated() {
    genText.value = ''
    genMeta.value = null
    genError.value = ''
    genStopped.value = false
  }

  // ==================== 版本历史 ====================

  const versions = ref([])
  const versionsBusy = ref(false)
  const restoring = ref(false)

  /** 列表只给版本号 / 来源 / 字数，正文靠 loadVersionContent 单独拉（一节可能几万字） */
  async function loadVersions() {
    if (!node.value) {
      versions.value = []
      return
    }
    versionsBusy.value = true
    try {
      const r = await apiFetch(`/studio/nodes/${node.value.id}/versions`)
      if (r.ok) versions.value = await r.json()
    } finally {
      versionsBusy.value = false
    }
  }

  async function loadVersionContent(versionId) {
    if (!node.value) return null
    const r = await apiFetch(`/studio/nodes/${node.value.id}/versions/${versionId}`)
    return r.ok ? await r.json() : null
  }

  /**
   * 回滚到某个版本。服务端会先把当前正文存一版，所以这里不需要额外传参数。
   * 调用方（StudioView）得先把编辑器里没落盘的草稿 flushSave 掉，
   * 否则那份改动既不在库里也到不了版本表，等于白打。
   */
  async function restoreVersion(versionId) {
    if (!node.value || restoring.value) return false
    restoring.value = true
    error.value = ''
    try {
      const r = await apiFetch(`/studio/nodes/${node.value.id}/versions/${versionId}/restore`, {
        method: 'POST',
      })
      if (!r.ok) {
        error.value = `回滚失败：HTTP ${r.status}`
        return false
      }
      node.value = await r.json()
      const t = tree.value.find((n) => n.id === node.value.id)
      if (t) t.word_count = node.value.word_count
      await loadVersions()
      return true
    } finally {
      restoring.value = false
    }
  }

  // ==================== 内容类型（content-types.yaml） ====================

  const contentTypes = ref([])
  const contentTypeError = ref('')

  /** 新增文种只改 YAML，这里只负责取回来渲染下拉框 */
  async function loadContentTypes() {
    const r = await apiFetch('/studio/content-types')
    if (!r.ok) return
    const j = await r.json()
    contentTypes.value = j.kinds || []
    contentTypeError.value = j.error || ''
  }

  // ==================== 大纲 ====================

  const outlineBusy = ref(false)

  /** 生成目录并落成节点树；已有节点时服务端会 409，需要 append=true */
  async function generateOutline({ instruction = '', sections = 0, append = false, model = genModel.value } = {}) {
    if (!work.value || outlineBusy.value) return null
    outlineBusy.value = true
    error.value = ''
    try {
      const r = await apiFetch(`/studio/works/${work.value.id}/outline`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction, sections, append, model }),
      })
      if (!r.ok) {
        let msg = `HTTP ${r.status}`
        try {
          const j = await r.json()
          msg = typeof j.detail === 'string' ? j.detail : (j.detail?.message || msg)
        } catch { /* 保持状态码文案 */ }
        error.value = msg
        return null
      }
      const result = await r.json()
      tree.value = (await (await apiFetch(`/studio/works/${work.value.id}/tree`)).json()) || []
      const first = tree.value.find((n) => n.ordinal != null)
      if (first) await selectNode(first.id)
      return result
    } finally {
      outlineBusy.value = false
    }
  }

  // ==================== 整篇流水线 ====================

  const pipeline = ref({
    running: false, total: 0, index: 0, title: '', chars: 0,
    done: 0, failed: [], elapsed: 0, error: '', preview: '',
    indexed: 0, summarized: 0, model: '',
  })

  /**
   * 逐节生成。用 fetch 读流（EventSource 不支持 POST），
   * 每节完成就刷一次树，用户能看到进度而不是干等。
   */
  async function runPipeline({ only_empty = true, limit = 0, instruction = '', model = genModel.value } = {}) {
    if (!work.value || pipeline.value.running) return null
    pipeline.value = {
      running: true, total: 0, index: 0, title: '', chars: 0,
      done: 0, failed: [], elapsed: 0, error: '', preview: '',
      indexed: 0, summarized: 0, model: '',
    }
    try {
      const resp = await apiFetch(`/studio/works/${work.value.id}/pipeline`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ only_empty, limit, instruction, model }),
      })
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`
        try { const j = await resp.json(); msg = typeof j.detail === 'string' ? j.detail : msg } catch { /* keep */ }
        pipeline.value.error = msg
        return null
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          for (const line of block.split('\n')) {
            if (!line.startsWith('data:')) continue
            const raw = line.slice(5).trim()
            if (!raw || raw === '[DONE]') continue
            let ev
            try { ev = JSON.parse(raw) } catch { continue }
            handlePipelineEvent(ev)
          }
        }
      }
    } catch (e) {
      pipeline.value.error = String(e?.message || e)
    } finally {
      pipeline.value.running = false
      // 收尾：刷新树与当前节点字数，避免侧栏还显示旧值
      tree.value = (await (await apiFetch(`/studio/works/${work.value.id}/tree`)).json()) || []
      if (node.value) await selectNode(node.value.id)
    }
    return pipeline.value
  }

  function handlePipelineEvent(ev) {
    const p = pipeline.value
    if (ev.event === 'plan') {
      p.total = ev.total || 0
      // 服务端报的是它实际选定的模型（「自动」时前端事先猜不到）
      p.model = ev.model || ''
      p.preview = ''
    } else if (ev.event === 'node_start') {
      p.index = ev.index || 0
      p.title = ev.title || ''
      p.preview = ''
    } else if (ev.event === 'delta') {
      p.preview = (p.preview + (ev.text || '')).slice(-4000)
      p.chars += (ev.text || '').length
    } else if (ev.event === 'node_done') {
      p.done += 1
      const t = tree.value.find((n) => n.id === ev.node_id)
      if (t) t.word_count = ev.chars || 0
    } else if (ev.event === 'node_indexed') {
      if (ev.chunks > 0) p.indexed += 1
    } else if (ev.event === 'node_summary') {
      p.summarized += 1
    } else if (ev.event === 'node_error' || ev.event === 'error') {
      if (ev.node_id) p.failed.push(ev.node_id)
      p.error = ev.message || '生成失败'
    } else if (ev.event === 'done') {
      p.done = ev.generated ?? p.done
      p.elapsed = ev.elapsed_ms || 0
      p.failed = ev.failed || p.failed
    }
  }

  // ==================== 索引 / 摘要（M2） ====================

  const indexStatus = ref(null)
  const indexing = ref(false)
  const summarizing = ref(false)
  const memoryNote = ref('')

  async function loadIndexStatus() {
    if (!work.value) return
    const r = await apiFetch(`/studio/works/${work.value.id}/index/status`)
    if (r.ok) indexStatus.value = await r.json()
  }

  async function reindex({ force = false } = {}) {
    if (!work.value || indexing.value) return null
    indexing.value = true
    memoryNote.value = ''
    try {
      const r = await apiFetch(`/studio/works/${work.value.id}/index`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force, include_sources: true }),
      })
      if (!r.ok) {
        memoryNote.value = `建索引失败：HTTP ${r.status}`
        return null
      }
      const j = await r.json()
      await loadIndexStatus()
      memoryNote.value = `已建 ${j.chunks_total} 块（重建 ${j.nodes} 节，跳过 ${j.skipped}）`
      return j
    } finally {
      indexing.value = false
    }
  }

  async function runSummarize({ scope = 'all', force = false, model = genModel.value } = {}) {
    if (!work.value || summarizing.value) return null
    summarizing.value = true
    memoryNote.value = '正在生成摘要…'
    try {
      const r = await apiFetch(`/studio/works/${work.value.id}/summarize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope, force, model }),
      })
      if (!r.ok) {
        memoryNote.value = `摘要失败：HTTP ${r.status}`
        return null
      }
      const j = await r.json()
      await loadIndexStatus()
      memoryNote.value = `节 ${j.nodes} / 讲卷 ${j.containers} / 全书 ${j.work}`
      return j
    } finally {
      summarizing.value = false
    }
  }

  // ==================== 拼装 / 导出 ====================

  const assembling = ref(false)

  /** 纯拼装，不调模型 */
  async function assembleDoc({ includeEmpty = false } = {}) {
    if (!work.value) return null
    assembling.value = true
    try {
      const r = await apiFetch(
        `/studio/works/${work.value.id}/assemble?include_empty=${includeEmpty ? 'true' : 'false'}`,
        { method: 'POST' },
      )
      if (!r.ok) return null
      return await r.json()
    } finally {
      assembling.value = false
    }
  }

  /** 导出要带口令，不能直接 <a href>，否则 401 */
  async function downloadExport({ includeEmpty = false } = {}) {
    if (!work.value) return null
    const r = await apiFetch(
      `/studio/works/${work.value.id}/export?include_empty=${includeEmpty ? 'true' : 'false'}`,
    )
    if (!r.ok) {
      error.value = `导出失败：HTTP ${r.status}`
      return null
    }
    const blob = await r.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${work.value.title || 'draft'}.md`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    return true
  }

  /**
   * 公众号版 HTML：全内联样式，直接写剪贴板就能粘进公众号编辑器。
   * theme 传空串表示用设置里存的默认版式；footer/archive 可以单独关掉。
   */
  async function exportHtml({ includeEmpty = false, theme = '', footer = true, archive = true } = {}) {
    if (!work.value) return ''
    const qs = new URLSearchParams({
      format: 'html',
      include_empty: String(includeEmpty),
      footer: String(footer),
      archive: String(archive),
    })
    if (theme) qs.set('theme', theme)
    const r = await apiFetch(`/studio/works/${work.value.id}/export?${qs}`)
    if (!r.ok) {
      error.value = await errDetail(r, '导出公众号版失败')
      return ''
    }
    return await r.text()
  }

  /** 发布前体检：排版、摘要、风险词，全部本地判，不调模型 */
  async function publishCheck() {
    if (!work.value) return null
    const r = await apiFetch(`/studio/works/${work.value.id}/publish-check`)
    if (!r.ok) {
      error.value = await errDetail(r, '发布检查失败')
      return null
    }
    return await r.json()
  }

  /** 让模型写公众号摘要（≤120 字），服务端直接写回 work.digest */
  async function writeDigest() {
    if (!work.value) return ''
    error.value = ''
    const r = await apiFetch(`/studio/works/${work.value.id}/digest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '写摘要失败')
      return ''
    }
    const data = await r.json()
    if (work.value) work.value = { ...work.value, digest: data.digest }
    return data.digest
  }

  // ==================== 公众号：文风 / 设置 / 标题 / 转发语 / 台账 ====================

  async function loadVoice() {
    const r = await apiFetch('/studio/voice')
    if (!r.ok) {
      error.value = await errDetail(r, '读取文风失败')
      return null
    }
    voice.value = await r.json()
    return voice.value
  }

  /** 贴一篇旧文：服务端加完就重算画像，这里只需要把新的拉回来 */
  async function addVoiceSample({ title = '', content }) {
    error.value = ''
    const r = await apiFetch('/studio/voice/samples', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, content }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '加入文风样本失败')
      return false
    }
    await loadVoice()
    return true
  }

  async function deleteVoiceSample(id) {
    const r = await apiFetch(`/studio/voice/samples/${id}`, { method: 'DELETE' })
    if (!r.ok) {
      error.value = await errDetail(r, '删除样本失败')
      return false
    }
    await loadVoice()
    return true
  }

  async function analyzeVoice() {
    const r = await apiFetch('/studio/voice/analyze', { method: 'POST' })
    if (!r.ok) {
      error.value = await errDetail(r, '重新统计失败')
      return false
    }
    await loadVoice()
    return true
  }

  async function saveVoiceProfile(profile) {
    const r = await apiFetch('/studio/voice/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '保存画像失败')
      return false
    }
    const j = await r.json()
    voice.value = { ...voice.value, profile: j.profile }
    return true
  }

  async function loadSettings() {
    const r = await apiFetch('/studio/settings')
    if (r.ok) settings.value = await r.json()
    return settings.value
  }

  async function saveSetting(key, value) {
    const r = await apiFetch(`/studio/settings/${key}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '保存设置失败')
      return false
    }
    settings.value = { ...settings.value, [key]: value }
    return true
  }

  /** 按全文出候选标题；返回 [{text, chars}] */
  async function generateTitles({ count = 6 } = {}) {
    if (!work.value) return []
    error.value = ''
    const r = await apiFetch(`/studio/works/${work.value.id}/titles`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count, model: genModel.value }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '换标题失败')
      return []
    }
    const j = await r.json()
    return j.titles || []
  }

  /** 朋友圈/群里的转发语；返回 [{angle, text, chars}] */
  async function shareCopies({ count = 3 } = {}) {
    if (!work.value) return []
    error.value = ''
    const r = await apiFetch(`/studio/works/${work.value.id}/share`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count, model: genModel.value }),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '写转发语失败')
      return []
    }
    const j = await r.json()
    return j.copies || []
  }

  async function loadPublications() {
    if (!work.value) return []
    const r = await apiFetch(`/studio/works/${work.value.id}/publications`)
    if (!r.ok) {
      error.value = await errDetail(r, '读取发布记录失败')
      return []
    }
    publications.value = await r.json()
    return publications.value
  }

  async function addPublication(payload) {
    if (!work.value) return false
    error.value = ''
    const r = await apiFetch(`/studio/works/${work.value.id}/publications`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '记发布失败')
      return false
    }
    await loadPublications()
    await loadPublicationSummary()
    return true
  }

  async function updatePublication(id, patch) {
    const r = await apiFetch(`/studio/publications/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    if (!r.ok) {
      error.value = await errDetail(r, '更新发布记录失败')
      return false
    }
    await loadPublications()
    await loadPublicationSummary()
    return true
  }

  async function deletePublication(id) {
    const r = await apiFetch(`/studio/publications/${id}`, { method: 'DELETE' })
    if (!r.ok) {
      error.value = await errDetail(r, '删除发布记录失败')
      return false
    }
    await loadPublications()
    await loadPublicationSummary()
    return true
  }

  /** 书架用：每个作品发了几篇、总阅读 */
  async function loadPublicationSummary() {
    const r = await apiFetch('/studio/publications/summary')
    if (r.ok) pubSummary.value = await r.json()
    return pubSummary.value
  }

  // ==================== 加工（多候选） ====================

  const transforming = ref(false)
  const transformResult = ref(null)

  async function transform({ text, action, candidates = 1, instruction = '', lang = '英文' }) {
    if (transforming.value) return null
    transforming.value = true
    transformResult.value = null
    error.value = ''
    try {
      const r = await apiFetch('/studio/transform', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text, action, candidates, instruction, lang,
          model: genModel.value,
          work_id: work.value?.id || '',
        }),
      })
      if (!r.ok) {
        let msg = `HTTP ${r.status}`
        try { const j = await r.json(); msg = j.detail?.message || j.detail || msg } catch { /* keep */ }
        error.value = msg
        return null
      }
      transformResult.value = await r.json()
      return transformResult.value
    } finally {
      transforming.value = false
    }
  }

  // ==================== 图谱与一致性（M3） ====================

  const graphData = ref(null) // { stats, entities, relations, foreshadows, cards }
  const graphBusy = ref(false)
  const graphNote = ref('')
  const checking = ref(false)
  const issueSummary = ref(null)
  const isNovel = computed(() => work.value?.kind === 'novel')

  async function loadGraph({ refresh = false } = {}) {
    if (!work.value || !isNovel.value) {
      graphData.value = null
      return
    }
    const q = refresh ? '?refresh=true' : ''
    const r = await apiFetch(`/studio/works/${work.value.id}/graph${q}`)
    if (r.ok) graphData.value = await r.json()
  }

  /** 抽取设定。整本用 all=true 分批推进，直到 remaining 为 0。 */
  async function extractGraph({ nodeId = '', all = false, force = false, limit = 5, model = genModel.value } = {}) {
    if (!work.value || graphBusy.value) return null
    graphBusy.value = true
    graphNote.value = ''
    try {
      const r = await apiFetch(`/studio/works/${work.value.id}/graph/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ node_id: nodeId, all, force, limit, model }),
      })
      if (!r.ok) {
        graphNote.value = `抽取失败：HTTP ${r.status} ${(await r.text()).slice(0, 160)}`
        return null
      }
      const j = await r.json()
      await loadGraph({ refresh: true })
      await loadIssueSummary()
      const bits = [`已完成 ${j.done} 节`]
      if (j.skipped) bits.push(`跳过 ${j.skipped}`)
      if (j.failed) bits.push(`失败 ${j.failed}`)
      if (j.remaining) bits.push(`还剩 ${j.remaining} 节`)
      bits.push(`+${j.entities} 实体 / +${j.relations} 关系 / +${j.foreshadows} 伏笔`)
      graphNote.value = bits.join(' · ')
      if (j.errors?.length) graphNote.value += `；${j.errors[0]}`
      return j
    } finally {
      graphBusy.value = false
    }
  }

  async function clearGraph() {
    if (!work.value) return false
    const r = await apiFetch(`/studio/works/${work.value.id}/graph`, { method: 'DELETE' })
    if (!r.ok) return false
    await loadGraph({ refresh: true })
    graphNote.value = '图谱已清空（设定卡保留）'
    return true
  }

  async function addCard({ kind, name, summary, dataJson = '{}', aliases = [] }) {
    if (!work.value) return false
    const r = await apiFetch(`/studio/works/${work.value.id}/cards`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind, name, summary, data_json: dataJson, aliases, pinned: true,
      }),
    })
    if (!r.ok) {
      graphNote.value = `建卡失败：HTTP ${r.status} ${(await r.text()).slice(0, 160)}`
      return false
    }
    await loadGraph({ refresh: true })
    return true
  }

  async function deleteCard(id) {
    await apiFetch(`/studio/cards/${id}`, { method: 'DELETE' })
    await loadGraph({ refresh: true })
  }

  /** 一致性检查：纯本地结构化比对，点一下就能跑。 */
  async function runCheck({ nodeId = '' } = {}) {
    if (!work.value || checking.value) return null
    checking.value = true
    try {
      const r = await apiFetch(`/studio/works/${work.value.id}/check`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ node_id: nodeId || '' }),
      })
      if (!r.ok) return null
      const j = await r.json()
      issueSummary.value = j.summary
      if (nodeId && node.value?.id === nodeId) {
        const ir = await apiFetch(`/studio/works/${work.value.id}/issues?node_id=${nodeId}`)
        if (ir.ok) issues.value = await ir.json()
      }
      return j
    } finally {
      checking.value = false
    }
  }

  async function resolveIssue(id) {
    const r = await apiFetch(`/studio/issues/${id}/resolve`, { method: 'POST' })
    if (!r.ok) return false
    issues.value = issues.value.filter((i) => i.id !== id)
    await loadIssueSummary()
    return true
  }

  async function loadIssueSummary() {
    if (!work.value) return
    const r = await apiFetch(`/studio/works/${work.value.id}/issues/summary`)
    if (r.ok) issueSummary.value = await r.json()
  }

  // ==================== 任务队列（M4） ====================

  const queue = ref(null)
  const queueBusy = ref(false)

  async function loadQueue() {
    if (!work.value) return null
    const r = await apiFetch(`/studio/queue/status?work_id=${work.value.id}`)
    if (r.ok) queue.value = await r.json()
    return queue.value
  }

  /** 批量入队：把「写哪些节」交给队列，关页面也能继续跑。 */
  async function enqueueBatch({
    taskType = 'draft', onlyEmpty = true, limit = 0, nodeIds = [],
    instruction = '', priority = 9, triageEvery = 0, model = genModel.value,
  } = {}) {
    if (!work.value || queueBusy.value) return null
    queueBusy.value = true
    try {
      const r = await apiFetch(`/studio/works/${work.value.id}/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_type: taskType, only_empty: onlyEmpty, limit,
          node_ids: nodeIds, instruction, priority, triage_every: triageEvery, model,
        }),
      })
      if (!r.ok) {
        error.value = `入队失败：HTTP ${r.status} ${(await r.text()).slice(0, 160)}`
        return null
      }
      const j = await r.json()
      queue.value = j.queue
      return j
    } finally {
      queueBusy.value = false
    }
  }

  async function pauseQueue(reason = '') {
    const r = await apiFetch(`/studio/queue/pause?reason=${encodeURIComponent(reason)}`, {
      method: 'POST',
    })
    if (r.ok) queue.value = await r.json()
    return r.ok
  }

  async function resumeQueue() {
    const r = await apiFetch('/studio/queue/resume', { method: 'POST' })
    if (r.ok) queue.value = await r.json()
    return r.ok
  }

  async function triageQueue(verdict) {
    const r = await apiFetch('/studio/queue/triage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ verdict }),
    })
    if (r.ok) queue.value = await r.json()
    return r.ok
  }

  async function retryJob(jobId) {
    const r = await apiFetch(`/studio/jobs/${jobId}/retry`, { method: 'POST' })
    if (r.ok) await loadQueue()
    return r.ok
  }

  const queueActive = computed(() => {
    const q = queue.value
    if (!q) return false
    if (q.paused || q.triage?.due) return true
    return Boolean(q.current) || (q.counts?.queued || 0) > 0
  })

  return {
    works, work, tree, node, stats, templates, sources, facts, terms, issues,
    loading, saving, error,
    generating, genText, genMeta, genError, genStopped, stopGenerate,
    genModel, genModelLabel, modelGroups, allGenModels, setGenModel, loadGenModels,
    activeWorkId, activeNodeId, template,
    loadTemplates, fetchWorks, createWork, updateWork, setWorkStatus, globalSearch,
    ideas, dailyWrites, loadIdeas, addIdea, useIdea, deleteIdea, loadDailyWrites,
    selectWork, selectNode, deleteWork,
    saveNode, createNode, importNodes, deleteNode, moveNode,
    loadSources, addSource, loadSource, searchSources, extractFacts, deleteSource, updateSource,
    loadFacts, addFact, deleteFact, updateFact,
    loadTerms, addTerm, deleteTerm, updateTerm,
    previewContext, generate, adoptGenerated, discardGenerated,
    // 版本历史
    versions, versionsBusy, restoring,
    loadVersions, loadVersionContent, restoreVersion,
    // M2 记忆
    indexStatus, indexing, summarizing, memoryNote,
    loadIndexStatus, reindex, runSummarize,
    // M1 尾款
    contentTypes, contentTypeError, loadContentTypes,
    outlineBusy, generateOutline,
    pipeline, runPipeline,
    assembling, assembleDoc, downloadExport, exportHtml, publishCheck, writeDigest,
    // 公众号第二轮：文风 / 设置 / 标题 / 转发语 / 台账
    voice, settings, publications, pubSummary,
    loadVoice, addVoiceSample, deleteVoiceSample, analyzeVoice, saveVoiceProfile,
    loadSettings, saveSetting, generateTitles, shareCopies,
    loadPublications, addPublication, updatePublication, deletePublication,
    loadPublicationSummary,
    transforming, transformResult, transform,
    // M3 图谱与一致性
    graphData, graphBusy, graphNote, checking, issueSummary, isNovel,
    loadGraph, extractGraph, clearGraph, addCard, deleteCard,
    runCheck, resolveIssue, loadIssueSummary,
    // M4 任务队列
    queue, queueBusy, queueActive,
    loadQueue, enqueueBatch, pauseQueue, resumeQueue, triageQueue, retryJob,
  }
})
