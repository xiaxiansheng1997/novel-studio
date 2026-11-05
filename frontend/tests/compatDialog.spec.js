/**
 * CompatDialog：直连通道配置弹窗的请求形状与三态语义。
 * 最要紧的是 api_key 的「不传=保持 / 输入=覆盖」——发错一次就把人库里存好的 key 洗没了。
 * apiFetch 整个换成替身：这里不起后端，端点本身的逻辑在 pytest（test_compat_admin.py）里。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const apiFetch = vi.hoisted(() => vi.fn())

vi.mock('../src/utils/api', () => ({ apiFetch }))

// eslint-disable-next-line import/first
import CompatDialog from '../src/components/CompatDialog.vue'

const yamlEntry = () => ({
  provider: 'openai',
  display_name: 'OpenAI 兼容示范',
  base_url: 'https://api.openai.com/v1',
  models: ['gpt-4o-mini'],
  enabled: true,
  key_source: 'env',
  key_masked: null,
})

const okJson = (body = { ok: true }) => ({
  ok: true,
  status: 200,
  json: async () => body,
})

const errJson = (detail, status = 400) => ({
  ok: false,
  status,
  json: async () => ({ detail }),
})

/** watch(open) false→true 才初始化草稿：直接 open:true 挂载拿不到 entry 回填。
 *  弹窗本体挂在 Teleport 里，wrapper 只剩两个注释桩——把 Teleport  stub 成
 *  透传组件，内容回到 wrapper 内，w.find 才拿得到真实 DOM。
 *  props 里要收掉 to：不然每个用例都刷一条「Extraneous non-props attributes」，
 *  真警告就被埋在这片噪声里。 */
const mountOpts = {
  global: {
    stubs: { Teleport: { props: ['to'], render() { return this.$slots.default?.() } } },
  },
}

async function openDialog(entry) {
  const w = mount(CompatDialog, { props: { open: false, entry }, ...mountOpts })
  await w.setProps({ open: true })
  await flushPromises()
  return w
}

beforeEach(() => {
  apiFetch.mockReset()
})

it('编辑打开：草稿回填条目，api_key 恒空（明文本来就不出后端）', async () => {
  const w = await openDialog(yamlEntry())
  const inputs = w.findAll('.cd-field input')
  // 编辑态无 key 字段：顺序是显示名、base_url、api_key；value 是 DOM property，得从 element 上读
  expect(inputs[1].element.value).toBe('https://api.openai.com/v1')
  expect(inputs[2].element.value).toBe('')
  expect(w.find('textarea').element.value).toBe('gpt-4o-mini')
})

it('保存 PUT：api_key 留空 = body 不带这个字段；模型按行拆开去空白', async () => {
  apiFetch.mockResolvedValue(okJson())
  const w = await openDialog(yamlEntry())
  await w.get('textarea').setValue('gpt-4o-mini\n\n deepseek-chat  ')
  await w.get('.cd-btn.primary').trigger('click')
  await flushPromises()
  const [url, opts] = apiFetch.mock.calls[0]
  expect(url).toBe('/admin/compat/openai')
  expect(opts.method).toBe('PUT')
  // Content-Type 必须显式带：真实 fetch 对字符串 body 不自动加，漏了 FastAPI 就 422
  expect(opts.headers).toEqual({ 'Content-Type': 'application/json' })
  const body = JSON.parse(opts.body)
  expect(body.api_key).toBeUndefined()
  expect(body.models).toEqual(['gpt-4o-mini', 'deepseek-chat'])
  expect(body.enabled).toBe(true)
  expect(w.emitted('saved')).toBeTruthy()
})

it('保存 PUT：填了新 key 才带 api_key', async () => {
  apiFetch.mockResolvedValue(okJson())
  const w = await openDialog(yamlEntry())
  await w.findAll('.cd-field input')[2].setValue('sk-abc123')
  await w.get('.cd-btn.primary').trigger('click')
  await flushPromises()
  expect(JSON.parse(apiFetch.mock.calls[0][1].body).api_key).toBe('sk-abc123')
})

it('新增走 POST：key 必填在前端先拦住，不发请求', async () => {
  const w = await openDialog(null)
  await w.get('.cd-btn.primary').trigger('click')
  await flushPromises()
  expect(apiFetch).not.toHaveBeenCalled()
  expect(w.text()).toContain('先给通道起个 key')
  expect(w.emitted('saved')).toBeFalsy()
})

it('新增走 POST：小写化 key，形状齐全', async () => {
  apiFetch.mockResolvedValue(okJson())
  const w = await openDialog(null)
  await w.get('.cd-field input').setValue('DeepSeek-API')
  await w.findAll('.cd-field input')[2].setValue('https://api.deepseek.com/v1')
  await w.get('textarea').setValue('deepseek-chat')
  await w.get('.cd-btn.primary').trigger('click')
  await flushPromises()
  const [url, opts] = apiFetch.mock.calls[0]
  expect(url).toBe('/admin/compat')
  expect(opts.method).toBe('POST')
  expect(opts.headers).toEqual({ 'Content-Type': 'application/json' })
  expect(JSON.parse(opts.body).key).toBe('deepseek-api')
})

it('保存被拒：显示后端 detail，不发 saved（不能让列表假装刷新成功）', async () => {
  apiFetch.mockResolvedValue(errJson('key 不能与反代 provider 同名'))
  const w = await openDialog(null)
  await w.get('.cd-field input').setValue('qwen')
  await w.get('.cd-btn.primary').trigger('click')
  await flushPromises()
  expect(w.find('.cd-msg.is-bad').text()).toContain('不能与反代 provider 同名')
  expect(w.emitted('saved')).toBeFalsy()
})

it('422 的数组 detail 也要摊平成人话，不能只剩 HTTP 422', async () => {
  apiFetch.mockResolvedValue({
    ok: false, status: 422,
    json: async () => ({ detail: [{ type: 'missing', loc: ['body'], msg: 'Field required' }] }),
  })
  const w = await openDialog(null)
  await w.get('.cd-field input').setValue('some-key')
  await w.get('.cd-btn.primary').trigger('click')
  await flushPromises()
  expect(w.find('.cd-msg.is-bad').text()).toContain('Field required')
})

it('测试连通：表单里没保存的 key 也带上（先验证再保存才有意义）', async () => {
  apiFetch.mockResolvedValue(okJson({ ok: true, latency_ms: 420 }))
  const w = await openDialog(yamlEntry())
  await w.findAll('.cd-field input')[2].setValue('sk-fresh')
  await w.get('.cd-btn').trigger('click')
  await flushPromises()
  const [url, opts] = apiFetch.mock.calls[0]
  expect(url).toBe('/admin/compat/openai/test')
  expect(JSON.parse(opts.body)).toEqual({ api_key: 'sk-fresh' })
  expect(w.text()).toContain('420 ms')
})

it('测试不通：ok=false 也走红色提示，转述上游原话', async () => {
  apiFetch.mockResolvedValue(okJson({ ok: false, detail: '上游返回 401' }))
  const w = await openDialog(yamlEntry())
  await w.get('.cd-btn').trigger('click')
  await flushPromises()
  expect(apiFetch.mock.calls[0][1].body).toBe('{}')
  expect(w.find('.cd-msg.is-bad').text()).toContain('上游返回 401')
})

// ==================== 从上游取模型 ====================

const MODELS = { ok: true, models: ['deepseek-chat', 'deepseek-reasoner', 'text-embedding-3'], total: 3, truncated: false, detail: '上游报了 3 个' }

/** 只接住取模型那一条，其余请求一律 404：多打一条少打一条都该被看见 */
function onlyModels(body = MODELS, status = 200) {
  apiFetch.mockImplementation(async (url, opts = {}) => {
    if (String(url).endsWith('/models')) return { ok: status === 200, status, json: async () => body }
    return errJson(`没 mock 的请求：${opts?.method || 'GET'} ${url}`, 404)
  })
}

const pickBox = (w, id) => w.findAll('.cd-pick-row').find((r) => r.text().includes(id))

it('取模型：表单里没保存的 base_url 与 key 一起递上去，路径用当前 key', async () => {
  onlyModels()
  const w = await openDialog(null)
  // 新建态多一个 key 输入框：顺序是 key、显示名、base_url、api_key
  await w.get('.cd-field input').setValue('Kimi-API')
  await w.findAll('.cd-field input')[2].setValue('https://api.moonshot.cn/v1')
  await w.findAll('.cd-field input')[3].setValue('sk-moon')
  await w.get('.cd-mini').trigger('click')
  await flushPromises()

  const [url, opts] = apiFetch.mock.calls[0]
  expect(url).toBe('/admin/compat/kimi-api/models')
  expect(opts.method).toBe('POST')
  expect(opts.headers).toEqual({ 'Content-Type': 'application/json' })
  expect(JSON.parse(opts.body)).toEqual({ base_url: 'https://api.moonshot.cn/v1', api_key: 'sk-moon' })
  expect(w.findAll('.cd-pick-row').map((r) => r.text())).toContain('deepseek-chat')
})

it('默认只列对话模型：embedding 这类藏起来，取消勾选就能看到', async () => {
  onlyModels()
  const w = await openDialog(yamlEntry())
  await w.get('.cd-label-row .cd-mini').trigger('click')
  await flushPromises()
  expect(w.findAll('.cd-pick-row')).toHaveLength(2)
  expect(w.text()).not.toContain('text-embedding-3')

  await w.get('.cd-pick-toggle input').setValue(false)
  expect(w.findAll('.cd-pick-row')).toHaveLength(3)
  expect(w.text()).toContain('text-embedding-3')
})

it('填入选中：textarea 换成勾选的那几个，手填而清单里没有的留着', async () => {
  onlyModels()
  const w = await openDialog(yamlEntry())
  await w.get('textarea').setValue('gpt-4o-preview')   // 上游清单里没有的手填项
  await w.get('.cd-label-row .cd-mini').trigger('click')
  await flushPromises()

  await pickBox(w, 'deepseek-chat').get('input').setValue(true)
  await w.get('.cd-pick-foot .cd-mini').trigger('click')
  await flushPromises()
  expect(w.get('textarea').element.value).toBe('deepseek-chat\ngpt-4o-preview')
  expect(w.find('.cd-msg').text()).toContain('留着')
  expect(w.find('.cd-pick').exists()).toBe(false)   // 收起来，不占地方
})

it('取一次不该把已有选择重置成全选：清单里与表单重的那几个预先勾上', async () => {
  onlyModels()
  const w = await openDialog(yamlEntry())
  await w.get('textarea').setValue('deepseek-reasoner')
  await w.get('.cd-label-row .cd-mini').trigger('click')
  await flushPromises()
  expect(w.findAll('.cd-pick-row input').map((i) => i.element.checked)).toEqual([false, true])
  expect(w.find('.cd-pick-count').text()).toContain('1 / 2')
})

it('上游不开放 /models：说清原因，textarea 不动，也不拦保存', async () => {
  onlyModels({ ok: false, models: [], detail: '上游的清单是空的（有些站点不开放 /models，模型 id 得手填）' })
  const w = await openDialog(yamlEntry())
  await w.get('textarea').setValue('本地模型')
  await w.get('.cd-label-row .cd-mini').trigger('click')
  await flushPromises()
  expect(w.find('.cd-pick .cd-msg.is-bad').text()).toContain('不开放 /models')
  expect(w.get('textarea').element.value).toBe('本地模型')

  apiFetch.mockClear()
  apiFetch.mockResolvedValue(okJson({ ok: true }))
  await w.get('.cd-btn.primary').trigger('click')
  await flushPromises()
  expect(apiFetch.mock.calls[0][0]).toBe('/admin/compat/openai')
  expect(JSON.parse(apiFetch.mock.calls[0][1].body).models).toEqual(['本地模型'])
})

it('新建态连 key 都没输：不发请求，直接说下一步', async () => {
  onlyModels()
  const w = await openDialog(null)
  await w.get('.cd-mini').trigger('click')
  await flushPromises()
  expect(apiFetch).not.toHaveBeenCalled()
  expect(w.find('.cd-pick .cd-msg.is-bad').text()).toContain('先给通道起个 key')
})
