/**
 * ImportDocDialog：拖进来的稿件怎么交给后端解、解完的话怎么如实说。
 * 盯三件容易退回去的事：
 *   1) .txt/.md 也必须走后端而不是浏览器 file.text()——后者只按 UTF-8 硬读，
 *      GBK 中文稿会解成一路不报错的乱码，等导进库才发现整本废了；
 *   2) 解不开时只摆后端那句人话，一个字都不写库；
 *   3) EPUB 的书名要回填「整篇算一节时的标题」，但用户手填过的不能覆盖。
 * store 换成替身：解析本身在 pytest（test_text_import.py）里测。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const store = vi.hoisted(() => ({
  fileToText: vi.fn(),
  importNodes: vi.fn(async () => ({ created: 0, failed: 0, firstId: '' })),
  importOutline: vi.fn(async () => ({ created: 0, failed: 0, volumes: 0, firstId: '' })),
}))

vi.mock('../src/stores/studio', () => ({ useStudioStore: () => store }))

// eslint-disable-next-line import/first
import ImportDocDialog from '../src/components/ImportDocDialog.vue'

const PARSED = (over = {}) => ({
  ok: true, text: '# 第一章 灯下\n\n她推开门。\n\n# 第二章 船\n\n雨还在下。',
  chars: 30, headings: 2, kind: 'txt', title: '', encoding: 'UTF-8', ...over,
})

const file = (name, body = 'x') => new File([body], name, { type: 'text/plain' })

async function openDialog(props = {}) {
  const w = mount(ImportDocDialog, { props: { open: false, ...props } })
  await w.setProps({ open: true })
  await flushPromises()
  return w
}

/** 走「拖进来」这条路：vue-test-utils 会把 payload 并进事件对象，组件只读 dataTransfer.files */
async function drop(w, f) {
  await w.get('label.drop').trigger('drop', { dataTransfer: { files: [f] } })
  await flushPromises()
}

beforeEach(() => {
  store.fileToText.mockReset()
  store.importNodes.mockReset()
  store.importNodes.mockResolvedValue({ created: 0, failed: 0, firstId: '' })
  store.importOutline.mockReset()
  store.importOutline.mockResolvedValue({ created: 0, failed: 0, volumes: 0, firstId: '' })
})

it('txt 也交后端解：不再用浏览器按 UTF-8 硬读', async () => {
  store.fileToText.mockResolvedValue(PARSED())
  const w = await openDialog()
  await drop(w, file('稿子.txt', 'GBK 字节'))
  expect(store.fileToText).toHaveBeenCalledTimes(1)
  expect(store.fileToText.mock.calls[0][0].name).toBe('稿子.txt')
  // 文件名与正文都交给后端，前端不自己 decode
  expect(w.find('textarea').element.value).toContain('她推开门')
  expect(w.text()).toContain('2 节')
})

it('不是 UTF-8 时把编码说出来：让用户知道这不是乱码而是按 GB18030 解的', async () => {
  store.fileToText.mockResolvedValue(PARSED({ encoding: 'GB18030' }))
  const w = await openDialog()
  await drop(w, file('旧稿.txt'))
  expect(w.find('.note-line').text()).toContain('已按 GB18030 解')
  expect(w.find('.warn-line').exists()).toBe(false)
})

it('编码没认出来要讲成「硬解的」，不能装作一切正常', async () => {
  store.fileToText.mockResolvedValue(PARSED({ encoding: 'unknown' }))
  const w = await openDialog()
  await drop(w, file('怪文件.txt'))
  expect(w.find('.note-line').text()).toContain('另存为 UTF-8')
})

it('EPUB：书名进提示并回填单节标题，手填过的不覆盖', async () => {
  store.fileToText.mockResolvedValue(PARSED({ kind: 'epub', title: '夜航船' }))
  const w = await openDialog()
  await drop(w, file('夜航船.epub'))
  expect(w.find('.note-line').text()).toContain('书名「夜航船」')
  expect(w.findAll('.fl input')[0].element.value).toBe('夜航船')

  store.fileToText.mockResolvedValue(PARSED({ kind: 'epub', title: '第二本' }))
  const w2 = await openDialog()
  await w2.findAll('.fl input')[0].setValue('我自己起的')
  await drop(w2, file('b.epub'))
  expect(w2.findAll('.fl input')[0].element.value).toBe('我自己起的')
})

it('后端拒了：原话摆出来，一个字都不写库', async () => {
  store.fileToText.mockResolvedValue({ ok: false, reason: '这不是有效的 .epub（epub 本质是 zip，请先确认文件没坏）' })
  const w = await openDialog()
  await drop(w, file('坏了.epub'))
  expect(w.find('.warn-line').text()).toContain('有效的 .epub')
  expect(w.find('textarea').element.value).toBe('')
  expect(store.importNodes).not.toHaveBeenCalled()
})

it('PDF / 老 doc 不发请求：后端也解不了，先说下一步怎么转', async () => {
  const w = await openDialog()
  await drop(w, file('书.pdf'))
  expect(store.fileToText).not.toHaveBeenCalled()
  expect(w.find('.warn-line').text()).toContain('Calibre')

  await drop(w, file('旧稿.doc'))
  expect(store.fileToText).not.toHaveBeenCalled()
  expect(w.find('.warn-line').text()).toContain('另存为 .docx')

  await drop(w, file('压缩包.zip'))
  expect(store.fileToText).not.toHaveBeenCalled()
  expect(w.find('.warn-line').text()).toContain('只支持')
})

it('解析阶段的底栏文案说的是解析，不是「正在导入 0 / 0」', async () => {
  let release
  store.fileToText.mockImplementation(() => new Promise((res) => { release = res }))
  const w = await openDialog()
  await w.get('label.drop').trigger('drop', { dataTransfer: { files: [file('长书.epub')] } })
  expect(w.find('.prog').text()).toBe('正在解析 epub…')
  release(PARSED({ kind: 'epub' }))
  await flushPromises()
  expect(w.find('.prog').exists()).toBe(false)
})

it('点导入按当前拆法逐节落库，全成才关窗', async () => {
  store.fileToText.mockResolvedValue(PARSED())
  store.importNodes.mockResolvedValue({ created: 2, failed: 0, firstId: 'n1' })
  const w = await openDialog({ parentId: 'v1', leafKind: 'chapter' })
  await drop(w, file('稿子.txt'))
  await w.get('.foot-act .btn-primary').trigger('click')
  await flushPromises()

  const [list, meta] = store.importNodes.mock.calls[0]
  expect(list.map((s) => s.title)).toEqual(['第一章 灯下', '第二章 船'])
  expect(meta).toEqual({ kind: 'chapter', parentId: 'v1' })
  expect(w.emitted('done')).toBeTruthy()
  expect(w.emitted('update:open')).toEqual([[false]])

  store.importNodes.mockResolvedValue({ created: 1, failed: 1, firstId: 'n1' })
  const w2 = await openDialog()
  await drop(w2, file('稿子.txt'))
  await w2.get('.foot-act .btn-primary').trigger('click')
  await flushPromises()
  expect(w2.find('.warn-line').text()).toContain('1 节没成功')
  expect(w2.emitted('update:open')).toBeFalsy()   // 没全成就不关窗，让人看清哪节掉了
})

it('部分失败要把哪几节、为什么没进来念出来：只报个数字等于没说', async () => {
  store.fileToText.mockResolvedValue(PARSED())
  store.importNodes.mockResolvedValue({
    created: 1, failed: 1, firstId: 'n1', reasons: ['第三章：标题为空'], stopped: false,
  })
  const w = await openDialog()
  await drop(w, file('稿子.txt'))
  await w.get('.foot-act .btn-primary').trigger('click')
  await flushPromises()
  const msg = w.find('.warn-line').text()
  expect(msg).toContain('导进去 1 节，1 节没成功')
  expect(msg).toContain('第三章：标题为空')
  expect(w.emitted('update:open')).toBeFalsy()
})

it('中途停了得说「后面的没发」：不然人以为整本都试过了', async () => {
  store.fileToText.mockResolvedValue(PARSED())
  store.importNodes.mockResolvedValue({
    created: 1, failed: 1, firstId: 'n1', reasons: ['第 1 节起没导进去：服务断了'], stopped: true,
  })
  const w = await openDialog()
  await drop(w, file('稿子.txt'))
  await w.get('.foot-act .btn-primary').trigger('click')
  await flushPromises()
  expect(w.find('.warn-line').text()).toContain('后面的节没发')
})

it('按标题层级：一级标题当卷、二级当章，计数和预览先摆出来再导', async () => {
  store.fileToText.mockResolvedValue(PARSED({
    kind: 'epub',
    headings: 5,
    text: '# 第一卷 起雾\n## 第一章 灯下\n她推开门。\n## 第二章 船\n雨还在下。\n'
      + '# 第二卷 逆流\n## 第三章 桥\n桥断了。',
  }))
  const w = await openDialog()
  await drop(w, file('夜航船.epub'))
  await w.findAll('select')[0].setValue('level')
  await flushPromises()

  const stat = w.find('.stat').text()
  expect(stat).toContain('2 卷')
  expect(stat).toContain('3 节')
  const rows = w.findAll('.preview li')
  expect(rows.map((r) => r.find('.pv-title').text())).toEqual(['第一卷 起雾', '第二卷 逆流'])
  expect(rows[0].find('.pv-title').classes()).toContain('is-vol')

  store.importOutline.mockResolvedValue({ created: 3, failed: 0, volumes: 2, firstId: 'n1' })
  await w.get('.foot-act .btn-primary').trigger('click')
  await flushPromises()

  const [outline, meta] = store.importOutline.mock.calls[0]
  expect(outline.map((v) => v.title)).toEqual(['第一卷 起雾', '第二卷 逆流'])
  expect(outline[0].children.map((c) => c.title)).toEqual(['第一章 灯下', '第二章 船'])
  expect(meta).toEqual({ kind: 'chapter' })
  expect(store.importNodes).not.toHaveBeenCalled()
  expect(w.emitted('done')[0][0].volumes).toBe(2)
})

it('自动不瞎分层：每章都写成 # 的 md 还是平铺，不会拆出两百个「卷」', async () => {
  store.fileToText.mockResolvedValue(PARSED({
    text: '# 第一章 灯下\n她推开门。\n# 第二章 船\n雨还在下。',
  }))
  const w = await openDialog()
  await drop(w, file('稿.md'))
  expect(w.find('.stat').text()).not.toContain('卷')

  store.importNodes.mockResolvedValue({ created: 2, failed: 0, firstId: 'n1' })
  await w.get('.foot-act .btn-primary').trigger('click')
  await flushPromises()
  expect(store.importNodes).toHaveBeenCalled()
  expect(store.importOutline).not.toHaveBeenCalled()
})

it('卷与章都写成一个 # 也分得开：靠「第X卷」与「第X章」认层级', async () => {
  store.fileToText.mockResolvedValue(PARSED({
    text: '# 第一卷 起雾\n# 第一章 灯下\n她推开门。\n# 第二章 船\n雨还在下。\n'
      + '# 第二卷 逆流\n# 第三章 桥\n桥断了。',
  }))
  const w = await openDialog()
  await drop(w, file('稿.md'))

  // 自动模式也得分：两卷都没卷首语，而且真不止一卷
  const stat = w.find('.stat').text()
  expect(stat).toContain('2 卷')
  expect(stat).toContain('3 节')
  expect(w.findAll('.preview .pv-title.is-vol').map((n) => n.text()))
    .toEqual(['第一卷 起雾', '第二卷 逆流'])
})

it('重开一次就清干净：上一本的正文不能留在草稿框里', async () => {
  store.fileToText.mockResolvedValue(PARSED())
  const w = await openDialog()
  await drop(w, file('第一本.txt'))
  expect(w.find('textarea').element.value).toContain('她推开门')
  await w.setProps({ open: false })
  await w.setProps({ open: true })
  expect(w.find('textarea').element.value).toBe('')
  expect(w.find('.note-line').exists()).toBe(false)
  expect(w.text()).toContain('上面还没有可导入的文字')
})
