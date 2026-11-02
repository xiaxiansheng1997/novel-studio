<template>
  <main ref="shelfEl" class="shelf">
    <header class="shelf-head">
      <div>
        <h1>写作台</h1>
        <p class="shelf-sub">长篇连载与单篇文档共用一套工作台，结构由作品类型决定。</p>
      </div>
      <div class="shelf-tools">
        <label class="shelf-search">
          <AppIcon name="search" :size="14" />
          <input v-model="shelfQ" class="field" placeholder="搜标题 / 主旨" aria-label="筛选作品" />
        </label>
        <button class="btn btn-sm" title="跨作品搜正文与素材（Ctrl+K）" @click="$emit('search', '')">
          <AppIcon name="search" :size="13" /> 搜正文
        </button>
        <button class="btn btn-sm" title="立项前先把题名与前提聊出来" @click="$emit('ask')">
          <AppIcon name="chat" :size="13" /> 先聊聊
        </button>
        <button class="btn btn-primary" @click="showCreate = !showCreate">
          <AppIcon name="plus" :size="15" /> 新建作品
        </button>
      </div>
    </header>

    <!-- 归档只是从书架收起，不删任何东西：所以在写/已归档是两个视图，不是筛选器 -->
    <div class="shelf-views">
      <button class="view-chip" :class="{ 'is-on': !showArchived }" @click="showArchived = false">
        在写 {{ activeWorks.length }}
      </button>
      <button class="view-chip" :class="{ 'is-on': showArchived }" @click="showArchived = true">
        已归档 {{ archivedWorks.length }}
      </button>
    </div>

    <!-- 写作日历：数据从 write_days 现算，不在别处另记一份 -->
    <section class="write-log" aria-label="写作统计">
      <span class="wl-item"><strong>{{ fmt(store.dailyWrites.today) }}</strong> 今天字数</span>
      <span class="wl-item"><strong>{{ store.dailyWrites.streak }}</strong> 连续天</span>
      <span class="wl-days" :title="last14.map((d) => `${d.day} ${d.chars} 字`).join(' / ')">
        <span
          v-for="d in last14"
          :key="d.day"
          class="wl-bar"
          :class="{ 'is-on': d.chars > 0 }"
          :style="{ height: barHeight(d.chars) }"
        ></span>
      </span>
    </section>

    <form v-if="showCreate" class="create card" @submit.prevent="create">
      <div class="create-row">
        <label class="fl">
          <span>类型</span>
          <select v-model="form.kind" class="field" @change="onKindChange">
            <option v-for="k in kinds" :key="k.id" :value="k.id">{{ k.label }}</option>
          </select>
        </label>
        <label v-if="scales.length > 1" class="fl">
          <span>规模</span>
          <select v-model="form.scale" class="field" @change="applyScaleDefaults">
            <option v-for="s in scales" :key="s.id" :value="s.id">{{ s.label }}</option>
          </select>
        </label>
        <label v-if="form.kind === 'novel'" class="fl">
          <span>题材</span>
          <input v-model="form.subtype" class="field" placeholder="玄幻 / 都市 / 科幻…" />
        </label>
        <label v-else-if="subtypes.length" class="fl">
          <span>文体</span>
          <select v-model="form.subtype" class="field">
            <option v-for="s in subtypes" :key="s.id" :value="s.id">{{ s.label }}</option>
          </select>
        </label>
        <label class="fl grow">
          <span>标题</span>
          <input v-model="form.title" class="field" required placeholder="作品标题" />
        </label>
      </div>
      <label class="fl grow">
        <span>一句话主旨</span>
        <textarea v-model="form.premise" class="field" rows="2"
                  placeholder="这篇/这本书要讲什么，给谁看，最重要的一句话是什么" />
      </label>
      <div class="create-foot">
        <label class="fl small">
          <span>目标字数</span>
          <input v-model.number="form.target_words" class="field" type="number" min="0" step="1000" />
        </label>
        <p v-if="store.error" class="err">{{ store.error }}</p>
        <button class="btn btn-primary" type="submit" :disabled="creating">
          {{ creating ? '创建中…' : '创建并进入' }}
        </button>
      </div>
    </form>

    <!-- 灵感箱：一句话想法先落这儿，不占作品 -->
    <section class="ideas">
      <div class="ideas-head">
        <h2>灵感箱</h2>
        <span class="ideas-sub">还没成形的一句话，先记着；「用它开写」会填进上面的新建表单</span>
      </div>
      <form class="ideas-add" @submit.prevent="saveIdea">
        <input v-model="ideaDraft" class="field" placeholder="一句话：想写什么，给谁看" />
        <button class="btn btn-sm" type="submit" :disabled="ideaBusy || !ideaDraft.trim()">记下</button>
      </form>
      <ul v-if="store.ideas.length" class="idea-list">
        <li v-for="i in store.ideas" :key="i.id" class="idea-row">
          <span class="idea-text" :title="i.text">{{ i.text }}</span>
          <button class="link-btn" @click="startFromIdea(i)">用它开写</button>
          <button class="idea-del" title="删掉这条想法" @click="store.deleteIdea(i.id)">删</button>
        </li>
      </ul>
    </section>

    <div v-if="visibleWorks.length" class="grid">
      <div
        v-for="w in visibleWorks"
        :key="w.id"
        class="work-card"
        role="button"
        tabindex="0"
        @click="$emit('open', w.id)"
        @keydown.enter="$emit('open', w.id)"
      >
        <button
          class="work-arch"
          :title="showArchived ? '移回「在写」' : '归档：从书架收起，正文与版本都不删'"
          @click.stop="showArchived ? unarchiveWork(w) : archiveWork(w)"
        >
          <AppIcon :name="showArchived ? 'refresh' : 'archive'" :size="12" />
        </button>
        <button
          class="work-del"
          title="删除作品（连同节点树与全部历史版本；删前留一份整库副本）"
          @click.stop="removeWork(w)"
        >
          <AppIcon name="trash" :size="12" />
        </button>
        <span class="work-kind">{{ kindLabel(w) }}</span>
        <span class="work-title">{{ w.title }}</span>
        <span class="work-meta">{{ w.node_count }} 节 · {{ fmt(w.words) }} 字</span>
        <span class="work-premise">{{ w.premise || '还没有主旨' }}</span>
      </div>
    </div>
    <p v-else-if="!showCreate" class="shelf-empty">
      <template v-if="shelfQ">
        没有匹配「{{ shelfQ }}」的作品。
        <button class="link-btn" @click="$emit('search', shelfQ)">去正文里搜</button>
      </template>
      <template v-else-if="showArchived">还没有归档的作品。归档只是从书架收起，不删正文。</template>
      <template v-else-if="archivedWorks.length">
        在写的都归档了（{{ archivedWorks.length }} 篇），切到「已归档」可以翻出来。
      </template>
      <template v-else>
        还没有作品。先点右上角「新建作品」，进去之后左栏有「导入旧稿」，能把已有章节一次贴进来。
      </template>
    </p>
  </main>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const emit = defineEmits(['open', 'ask', 'search', 'notice'])

const props = defineProps({
  // 「先聊聊」的回填：父层每回填一次就换一个新对象，watcher 靠身份变化触发
  seed: { type: Object, default: null },
})

const store = useStudioStore()

const showCreate = ref(false)
const creating = ref(false)
const form = ref({ kind: 'article', scale: 'single', subtype: 'tech', title: '', premise: '', target_words: 5000 })

// 书目搜索只筛卡片：跨作品搜正文走 Ctrl+K 那个弹层，两种搜索混在一个框里会互相误会
const shelfQ = ref('')
const showArchived = ref(false)

const ideaDraft = ref('')
const ideaBusy = ref(false)
const pendingIdea = ref('')
const shelfEl = ref(null)

// 类型下拉来自 /studio/content-types（content-types.yaml），新增文种只改 YAML
const kinds = computed(() =>
  store.contentTypes.length
    ? store.contentTypes
    : [{ id: 'article', label: '文档', scales: [{ id: 'single', label: '单篇' }], subtypes: [] }],
)
const currentKind = computed(() => kinds.value.find((k) => k.id === form.value.kind) || null)
const scales = computed(() => currentKind.value?.scales || [])
const subtypes = computed(() => currentKind.value?.subtypes || [])

// 关掉新建表单就别再记着那条灵感：否则下一次手动新建会被误标成「用它开写」
watch(showCreate, (on) => {
  if (!on) pendingIdea.value = ''
})

// content-types 到货后再对一次默认值：首屏渲染时它还是空数组
watch(kinds, () => {
  if (!kinds.value.some((k) => k.id === form.value.kind)) form.value.kind = kinds.value[0]?.id || 'article'
  applyScaleDefaults()
}, { immediate: true })

/** 「先聊聊」的结果回填新建表单：只是草稿，建不建、怎么改都还由人定 */
watch(() => props.seed, (s) => {
  if (!s) return
  form.value.title = String(s.title || '').slice(0, 60)
  form.value.premise = String(s.premise || '').slice(0, 400)
  showCreate.value = true
  nextTick(() => shelfEl.value?.scrollTo({ top: 0, behavior: 'smooth' }))
})

const activeWorks = computed(() => store.works.filter((w) => (w.status || 'active') !== 'archived'))
const archivedWorks = computed(() => store.works.filter((w) => w.status === 'archived'))
const visibleWorks = computed(() => {
  const base = showArchived.value ? archivedWorks.value : activeWorks.value
  const q = shelfQ.value.trim().toLowerCase()
  if (!q) return base
  return base.filter((w) =>
    (w.title || '').toLowerCase().includes(q) || (w.premise || '').toLowerCase().includes(q),
  )
})

function onKindChange() {
  applyScaleDefaults()
  form.value.subtype = subtypes.value[0]?.id || ''
}

function applyScaleDefaults() {
  form.value.scale = scales.value[0]?.id || 'single'
  const s = scales.value.find((x) => x.id === form.value.scale)
  if (s?.target_words) form.value.target_words = s.target_words
}

async function saveIdea() {
  const text = ideaDraft.value.trim()
  if (!text || ideaBusy.value) return
  ideaBusy.value = true
  const idea = await store.addIdea(text)
  ideaBusy.value = false
  if (!idea) {
    emit('notice', store.error || '记下失败', true)
    return
  }
  ideaDraft.value = ''
}

/** 用它开写：只填新建表单，kind/scale 仍由人挑——猜错类型比不填更麻烦 */
async function startFromIdea(idea) {
  pendingIdea.value = idea.id
  form.value.title = idea.text.slice(0, 40)
  form.value.premise = idea.text
  showCreate.value = true
  await nextTick()
  shelfEl.value?.scrollTo({ top: 0, behavior: 'smooth' })
}

async function archiveWork(w) {
  if (!(await store.setWorkStatus(w.id, 'archived'))) emit('notice', store.error || '归档失败', true)
}

async function unarchiveWork(w) {
  if (!(await store.setWorkStatus(w.id, 'active'))) emit('notice', store.error || '恢复失败', true)
}

async function removeWork(w) {
  if (!window.confirm(
    `删除《${w.title}》？会连同它的节点树、正文与全部历史版本一并删掉。\n\n`
    + '删之前会先拷一份整库副本到 storage/backups/works/，要找回只能停服务把那个文件盖回去。')) return
  if (!(await store.deleteWork(w.id))) emit('notice', store.error || '删除失败', true)
}

async function create() {
  creating.value = true
  store.error = ''
  try {
    const w = await store.createWork({ ...form.value })
    // 从灵感箱开的稿：记下指向，之后在库里能回看这句想法变成了哪篇
    if (pendingIdea.value) {
      await store.useIdea(pendingIdea.value, w.id)
      pendingIdea.value = ''
    }
    showCreate.value = false
    form.value = { kind: 'article', scale: 'single', subtype: 'tech', title: '', premise: '', target_words: 5000 }
    emit('open', w.id)
  } catch (e) {
    store.error = String(e?.message || e).slice(0, 200)
  } finally {
    creating.value = false
  }
}

function fmt(n) {
  if (!n) return '0'
  return n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(n)
}

function kindLabel(w) {
  if (w.kind === 'novel') return '长篇'
  return w.scale === 'series' ? '系列' : (w.subtype || '单篇')
}

// ---- 写作日历（近 14 天，今天在最右）----
function ymd(dt) {
  const m = String(dt.getMonth() + 1).padStart(2, '0')
  const d = String(dt.getDate()).padStart(2, '0')
  return `${dt.getFullYear()}-${m}-${d}`
}

const last14 = computed(() => {
  const got = new Map((store.dailyWrites.days || []).map((d) => [d.day, d.chars]))
  const out = []
  const base = new Date()
  for (let i = 13; i >= 0; i--) {
    const dt = new Date(base.getFullYear(), base.getMonth(), base.getDate() - i)
    const day = ymd(dt)
    out.push({ day, chars: got.get(day) || 0 })
  }
  return out
})

const dailyMax = computed(() => Math.max(1, ...last14.value.map((d) => d.chars)))

function barHeight(chars) {
  if (!chars) return '3px'
  return Math.max(6, Math.round((chars / dailyMax.value) * 24)) + 'px'
}
</script>

<style scoped>
/* ---------- 书架 ---------- */
.shelf {
  flex: 1;
  overflow-y: auto;
  padding: 34px 40px 60px;
  max-width: 1080px;
  margin: 0 auto;
  width: 100%;
}
.shelf-head {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 22px;
}
.shelf-head h1 {
  margin: 0 0 5px;
  font-family: theme('fontFamily.serif');
  font-size: 26px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text);
}
.shelf-sub {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-faint);
}
.shelf-tools {
  margin-left: auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
}
.shelf-search {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 9px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--panel);
  color: var(--text-faint);
}
.shelf-search:focus-within {
  border-color: rgba(var(--run-rgb), 0.45);
}
.shelf-search .field {
  width: 168px;
  height: 26px;
  padding: 0;
  border: none;
  background: transparent;
  font-size: 12px;
}
.shelf-search .field:focus {
  outline: none;
}
.shelf-views {
  display: flex;
  gap: 6px;
  margin: -6px 0 18px;
}
.view-chip {
  height: 26px;
  padding: 0 11px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  color: var(--text-faint);
  font-size: 12px;
  font-family: theme('fontFamily.mono');
  cursor: pointer;
  transition: color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.view-chip:hover {
  color: var(--text);
  background: rgba(var(--over), 0.04);
}
.view-chip.is-on {
  /* 主色当小字用必须走 --run-fg：--run 是填充/描边档，在带色的底上不够 */
  color: var(--run-fg);
  background: rgba(var(--run-rgb), 0.1);
}
.write-log {
  display: flex;
  align-items: center;
  gap: 16px;
  margin: -6px 0 14px;
  padding: 8px 12px;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-control);
  background: var(--panel);
  font-size: 11.5px;
  color: var(--text-faint);
}
.wl-item strong {
  margin-right: 4px;
  font-family: theme('fontFamily.mono');
  font-size: 14px;
  color: var(--text);
}
.wl-days {
  margin-left: auto;
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 26px;
}
.wl-bar {
  width: 4px;
  min-height: 3px;
  border-radius: 1px;
  background: rgba(var(--over), 0.09);
}
.wl-bar.is-on {
  background: rgba(var(--run-rgb), 0.65);
}
.ideas {
  margin-bottom: 18px;
  padding: 12px 14px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-panel);
}
.ideas-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 8px;
}
.ideas-head h2 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.ideas-sub {
  font-size: 11px;
  color: var(--text-faint);
}
.ideas-add {
  display: flex;
  gap: 8px;
}
.ideas-add .field {
  flex: 1;
  min-width: 0;
}
.idea-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 150px;
  overflow-y: auto;
}
.idea-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: 5px;
}
.idea-row:hover {
  background: rgba(var(--over), 0.035);
}
.idea-text {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.idea-del {
  border: none;
  background: transparent;
  color: var(--text-faint);
  font-size: 11px;
  cursor: pointer;
}
.idea-del:hover {
  color: var(--danger);
}
.link-btn {
  margin-left: 4px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--run-fg);
  font-size: inherit;
  font-family: inherit;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.link-btn:hover {
  color: var(--text);
}
.create {
  padding: 16px 18px;
  margin-bottom: 22px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.create-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.fl {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.fl > span {
  font-size: 11px;
  color: var(--text-faint);
}
.fl .field {
  width: 132px;
}
.fl.grow {
  flex: 1;
  min-width: 200px;
}
.fl.grow .field,
.fl > .field.grow {
  width: 100%;
}
.fl.small .field {
  width: 110px;
}
.create-foot {
  display: flex;
  align-items: flex-end;
  gap: 12px;
}
.create-foot .btn {
  margin-left: auto;
}
.err {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
.work-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 15px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.work-del {
  position: absolute;
  top: 8px;
  right: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s var(--ease), color 0.15s var(--ease);
}
.work-arch {
  position: absolute;
  top: 8px;
  right: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s var(--ease), color 0.15s var(--ease);
}
.work-card:hover .work-arch,
.work-card:focus-within .work-arch {
  opacity: 1;
}
.work-arch:hover {
  color: var(--run);
  background: rgba(var(--run-rgb), 0.12);
}
.work-card:hover .work-del,
.work-card:focus-within .work-del {
  opacity: 1;
}
.work-del:hover {
  color: var(--danger);
  background: rgba(var(--danger-rgb), 0.1);
}
.work-card:hover {
  border-color: rgba(var(--run-rgb), 0.45);
  background: var(--panel-2);
}
.work-kind {
  font-family: theme('fontFamily.mono');
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--run-fg);
}
.work-title {
  font-family: theme('fontFamily.serif');
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
}
.work-meta {
  font-size: 11px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.work-premise {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.shelf-empty {
  margin-top: 30px;
  font-size: 13px;
  color: var(--text-faint);
}
</style>
