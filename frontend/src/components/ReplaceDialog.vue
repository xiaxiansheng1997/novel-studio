<template>
  <div v-if="open" class="mask" @click.self="close">
    <div class="box" role="dialog" aria-modal="true" aria-label="查找替换">
      <header class="head">
        <AppIcon name="swap" :size="15" />
        <span class="head-title">查找替换</span>
        <span class="head-sub">{{ scopeLabel }}</span>
        <button class="esc" title="关闭（Esc）" @click="close">
          <span>Esc</span>
          <AppIcon name="x" :size="12" />
        </button>
      </header>

      <div class="body">
        <p class="tip">
          默认只做预览，不点「替换」一个字都不写库。真替换时，改前的那一版会先存进这一节的
          「历史」，改错了还能回滚。
        </p>

        <div class="line">
          <input
            v-model="find"
            class="field"
            maxlength="200"
            placeholder="查找：人名、说法、错字"
            aria-label="查找内容"
            @input="invalidate"
            @keydown.enter.prevent="doPreview"
          />
          <AppIcon name="swap" :size="13" class="arrow" />
          <input
            v-model="replText"
            class="field"
            maxlength="200"
            placeholder="替换为：留空就是删掉这个说法"
            aria-label="替换为"
            @input="invalidate"
            @keydown.enter.prevent="doPreview"
          />
        </div>

        <div class="row">
          <span class="rlabel">范围</span>
          <div class="seg" role="group" aria-label="替换范围">
            <button
              type="button"
              class="chip"
              :class="{ 'is-on': scope === 'work' }"
              title="整本书里有正文的节都算，与导出、整本检查同一个范围"
              @click="pick('scope', 'work')"
            >整本书</button>
            <button
              type="button"
              class="chip"
              :class="{ 'is-on': scope === 'node' }"
              :disabled="!nodeId"
              :title="nodeTitle ? `只动「${nodeTitle}」` : '没有选中的节'"
              @click="pick('scope', 'node')"
            >本节</button>
          </div>
          <span class="rlabel">大小写</span>
          <div class="seg" role="group" aria-label="是否区分大小写">
            <button
              type="button"
              class="chip"
              :class="{ 'is-on': caseSensitive }"
              @click="pick('case', true)"
            >区分</button>
            <button
              type="button"
              class="chip"
              :class="{ 'is-on': !caseSensitive }"
              title="只用于定位命中，替换不会把正文其它部分改成小写"
              @click="pick('case', false)"
            >忽略</button>
          </div>
        </div>

        <div class="row">
          <span class="rlabel">术语</span>
          <div class="seg" role="group" aria-label="术语表要不要跟着改">
            <button
              type="button"
              class="chip"
              :class="{ 'is-on': updateTerms }"
              title="术语表里与「查找」完全同名的那一条一起改名"
              @click="pick('terms', true)"
            >跟着改名</button>
            <button
              type="button"
              class="chip"
              :class="{ 'is-on': !updateTerms }"
              @click="pick('terms', false)"
            >不动</button>
          </div>
          <p class="row-tip">图谱里的实体名与设定卡不在这里改，那要重跑抽取。</p>
        </div>

        <button class="btn btn-sm pv-btn" :disabled="busy" @click="doPreview">
          <AppIcon name="search" :size="12" />
          {{ busy && kind === 'preview' ? '预览中…' : '预览命中' }}
        </button>

        <p v-if="error" class="warn-line">{{ error }}</p>

        <template v-if="found">
          <p class="stat">
            命中 <strong>{{ found.total_hits }}</strong> 处，分布在
            <strong>{{ found.total_nodes }}</strong> 节（看了 {{ found.scanned_nodes }} 节）
            <template v-if="found.term_hits"> · 术语表里有 {{ found.term_hits }} 条同名</template>
          </p>

          <ol v-if="found.matches.length" class="hits">
            <li v-for="m in hitList" :key="m.node_id">
              <span class="hit-ord">{{ m.ordinal }}</span>
              <span class="hit-title">{{ m.title || '未命名' }}</span>
              <span class="hit-count">{{ m.count }} 处</span>
              <span class="hit-delta">{{ charDelta(m) }}</span>
            </li>
            <li v-if="found.matches.length > hitList.length" class="hit-more">
              还有 {{ found.matches.length - hitList.length }} 节没展开
            </li>
          </ol>

          <ul v-if="found.warnings.length" class="warns">
            <li v-for="(w, i) in found.warnings" :key="i">{{ w }}</li>
          </ul>
        </template>
        <p v-else-if="find.trim()" class="quiet">还没预览：先点一次，看看会动到哪几节。</p>
      </div>

      <footer class="foot">
        <span class="foot-tip">{{ footTip }}</span>
        <div class="foot-act">
          <button class="btn btn-sm" :disabled="busy" @click="close">取消</button>
          <button class="btn btn-primary btn-sm" :disabled="!canApply" @click="apply">
            {{ busy && kind === 'apply' ? '替换中…' : applyLabel }}
          </button>
        </div>
      </footer>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  open: { type: Boolean, default: false },
  // 「本节」这一档要有当前选中的节才点得动；没有就只留整本书
  nodeId: { type: String, default: '' },
  nodeTitle: { type: String, default: '' },
})
const emit = defineEmits(['update:open', 'done'])
const store = useStudioStore()

const find = ref('')
const replText = ref('')
const scope = ref('work')
const caseSensitive = ref(true)
const updateTerms = ref(true)
const found = ref(null)
const previewKey = ref('')
const busy = ref(false)
const kind = ref('')
const error = ref('')

/** 表单签名：预览之后又改了任何一个条件，那一份预览就不算数了 */
const signature = computed(() => JSON.stringify([
  find.value, replText.value, scope.value, caseSensitive.value, updateTerms.value, props.nodeId,
]))

const scopeLabel = computed(() =>
  (scope.value === 'node' ? props.nodeTitle || '这一节' : '整本书'),
)
const canApply = computed(() =>
  Boolean(found.value)
  && previewKey.value === signature.value
  && found.value.total_hits > 0,
)
const applyLabel = computed(() =>
  (canApply.value ? `替换 ${found.value.total_hits} 处` : '先预览'),
)
const hitList = computed(() => (found.value?.matches || []).slice(0, 8))
const footTip = computed(() => {
  if (!found.value) return '默认只预览，不点「替换」一个字都不写库'
  if (previewKey.value !== signature.value) return '刚改过条件，那份预览不算数了：再点一次'
  if (!found.value.total_hits) return '没有可替换的地方'
  return '替换前会把改前的那一版存进「历史」，正文一改摘要就判过期'
})

/** 替换前后每节的字数差：一眼能看出这次改动把稿子改胖还是改瘦 */
function charDelta(m) {
  const d = (m.chars_after || 0) - (m.chars_before || 0)
  if (!d) return '字数不变'
  return `${d > 0 ? '+' : ''}${d} 字`
}

function pick(which, next) {
  const cur = which === 'scope'
    ? scope.value
    : which === 'case' ? caseSensitive.value : updateTerms.value
  // 再点已经选中那一档：什么都不动。清掉预览只能算改过条件
  if (cur === next) return
  if (which === 'scope') scope.value = next
  else if (which === 'case') caseSensitive.value = next
  else updateTerms.value = next
  invalidate()
}

function invalidate() {
  found.value = null
  previewKey.value = ''
  error.value = ''
}

function body(dryRun) {
  return {
    find: find.value,
    replace: replText.value,
    scope: scope.value,
    // 整本书时后端忽略这个字段；带上空串省得分支
    node_id: scope.value === 'node' ? props.nodeId : '',
    case_sensitive: caseSensitive.value,
    dry_run: dryRun,
    update_terms: updateTerms.value,
  }
}

async function doPreview() {
  if (busy.value) return
  if (!find.value.trim()) {
    error.value = '查找是空的：空串不算命中，服务端会直接拒掉'
    return
  }
  busy.value = true
  kind.value = 'preview'
  error.value = ''
  try {
    const res = await store.replaceText(body(true))
    if (!res) {
      found.value = null
      previewKey.value = ''
      error.value = store.error || '预览失败：确认服务还在跑'
      return
    }
    found.value = res
    previewKey.value = signature.value
  } finally {
    busy.value = false
    kind.value = ''
  }
}

async function apply() {
  if (!canApply.value || busy.value) return
  busy.value = true
  kind.value = 'apply'
  error.value = ''
  try {
    const res = await store.replaceText(body(false))
    if (!res) {
      error.value = store.error || '替换失败：稿子没动，可以再试一次'
      return
    }
    emit('done', res)
    // 这里不能走 close()：它开头拦 busy，而 busy 要等 finally 才复位，
    // 那样弹层会一次都关不上，遮罩一直挡着页面
    emit('update:open', false)
  } finally {
    busy.value = false
    kind.value = ''
  }
}

function close() {
  if (busy.value) return
  emit('update:open', false)
}

function onKey(e) {
  if (e.key === 'Escape' && props.open && !busy.value) {
    e.preventDefault()
    close()
  }
}

// 重新打开只清预览：查找与替换那两格留着——改完发现还有个别名没换是常事，
// 而上一轮的命中列表已经对不上这一轮的词，留着只会误导
watch(() => props.open, (on) => {
  if (!on) return
  invalidate()
  if (scope.value === 'node' && !props.nodeId) scope.value = 'work'
})

// 切节等于换了「本节」指的是哪一节：预览跟着作废
watch(() => props.nodeId, () => invalidate())

onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 140;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--overlay);
}
.box {
  width: min(680px, 100%);
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-stage, 16px);
  background: var(--panel);
  box-shadow: 0 30px 80px rgba(var(--shade-rgb), 0.5);
  overflow: hidden;
}
.head {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-faint);
}
.head-title {
  font-size: 13px;
  color: var(--text);
}
.head-sub {
  font-size: 11px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
}
.esc {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  height: 24px;
  padding: 0 7px;
  border: 1px solid var(--line-soft);
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  font-size: 10px;
  font-family: theme('fontFamily.mono');
  cursor: pointer;
}
.esc:hover {
  color: var(--text);
  border-color: var(--line);
}
.body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tip {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-faint);
}
.line {
  display: grid;
  grid-template-columns: 1fr 16px 1fr;
  align-items: center;
  gap: 8px;
}
.arrow {
  color: var(--text-faint);
  justify-self: center;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rlabel {
  font-size: 11px;
  color: var(--text-faint);
}
.seg {
  display: flex;
  gap: 3px;
}
/* 档位控件沿用排版弹层那套 chip：同一个库里的手感不该有两套长相 */
.chip {
  height: 22px;
  min-width: 30px;
  padding: 0 8px;
  border: 1px solid var(--line-soft);
  border-radius: 6px;
  background: rgba(var(--over), 0.03);
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
  transition: color 0.16s var(--ease), border-color 0.16s var(--ease),
    background-color 0.16s var(--ease);
}
.chip:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--line);
}
.chip:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.chip.is-on {
  border-color: rgba(var(--run-rgb), 0.5);
  background: rgba(var(--run-rgb), 0.14);
  color: var(--run-fg);
}
.row-tip {
  margin: 0;
  font-size: 10.5px;
  color: var(--text-faint);
}
.pv-btn {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.stat {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
}
.stat strong {
  color: var(--run);
  font-family: theme('fontFamily.mono');
}
.hits {
  margin: 0;
  padding: 0;
  list-style: none;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  overflow: hidden;
}
.hits li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 6px 9px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 12px;
}
.hits li:last-child {
  border-bottom: none;
}
.hit-ord {
  flex-shrink: 0;
  min-width: 22px;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
}
.hit-title {
  flex-shrink: 0;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
}
.hit-count {
  margin-left: auto;
  flex-shrink: 0;
  color: var(--text-muted);
  font-family: theme('fontFamily.mono');
  font-size: 11px;
}
.hit-delta {
  flex-shrink: 0;
  min-width: 54px;
  text-align: right;
  color: var(--text-faint);
  font-family: theme('fontFamily.mono');
  font-size: 10.5px;
}
.hit-more {
  justify-content: center;
  color: var(--text-faint);
}
.warns {
  margin: 0;
  padding: 8px 10px 8px 24px;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  background: rgba(var(--warn-rgb), 0.07);
  list-style: disc;
}
.warns li {
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-muted);
}
.warns li + li {
  margin-top: 4px;
}
.warn-line {
  margin: 0;
  font-size: 12px;
  color: var(--danger-fg);
}
.quiet {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-faint);
}
.foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-top: 1px solid var(--line-soft);
}
.foot-tip {
  font-size: 11.5px;
  color: var(--text-faint);
}
.foot-act {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
</style>
