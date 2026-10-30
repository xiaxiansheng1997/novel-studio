<template>
  <div v-if="open && version" class="mask" @click.self="close">
    <section class="dlg" role="dialog" aria-modal="true" :aria-label="`对比版本 ${version.version_no}`">
      <header class="dlg-head">
        <div class="head-txt">
          <h2>
            版本 #{{ version.version_no }}
            <span class="v-src">{{ sourceLabel }}</span>
          </h2>
          <p class="dlg-sub">
            {{ fmtTime(version.created_at) }} · {{ version.chars }} 字
            <template v-if="identical">· 与当前正文一致，不需要回滚</template>
            <template v-else>
              · 回滚后 <em class="v-add">找回 {{ diff.added }}</em> 行、
              <em class="v-del">舍掉 {{ diff.removed }}</em> 行
            </template>
            <span v-if="version.run_id" class="v-run" :title="version.run_id">来自生成</span>
          </p>
        </div>
        <div class="head-acts">
          <button
            class="btn btn-primary btn-sm"
            :disabled="identical || store.restoring"
            @click="doRestore"
          >
            <AppIcon name="check" :size="13" />
            {{ store.restoring ? '回滚中…' : '用这一版' }}
          </button>
          <button class="icon-btn" title="关闭" @click="close">
            <AppIcon name="x" :size="15" />
          </button>
        </div>
      </header>

      <div class="panes">
        <div v-for="pane in panes" :key="pane.title" class="pane">
          <div class="pane-head">
            <span>{{ pane.title }}</span>
            <span class="pane-meta">{{ pane.count }} 行</span>
          </div>
          <div class="pane-body">
            <p
              v-for="(r, i) in pane.rows"
              :key="i"
              class="ln"
              :class="r.type"
            >{{ r.text === '' ? ' ' : r.text }}</p>
            <p v-if="!pane.rows.length" class="ln same">（这一侧没有正文）</p>
          </div>
        </div>
      </div>

      <footer class="dlg-foot">
        <p class="foot-tip">
          点「用这一版」会先把当前正文存成一个版本再写回这一版，改过的摘要同时重新判过期。
        </p>
      </footer>
    </section>
  </div>
</template>

<script setup>
import { computed, onUnmounted, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { currentRows, diffText, versionRows } from '../utils/diff'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  open: { type: Boolean, default: false },
  // version 是 get_version 的返回（带 content），由面板拉好再传进来
  version: { type: Object, default: null },
  current: { type: String, default: '' },
})
const emit = defineEmits(['update:open', 'restore'])

const store = useStudioStore()

const SOURCE_LABEL = { ai_draft: 'AI 草稿', ai_polish: 'AI 润色', human: '手改' }
const sourceLabel = computed(() => SOURCE_LABEL[props.version?.source] || props.version?.source || '未知')

const diff = computed(() => diffText(props.current, props.version?.content || ''))
const identical = computed(() => diff.value.added === 0 && diff.value.removed === 0)

const panes = computed(() => [
  {
    title: '当前正文',
    rows: currentRows(diff.value.rows),
    count: currentRows(diff.value.rows).length,
  },
  {
    title: `版本 #${props.version?.version_no ?? ''}（这一版）`,
    rows: versionRows(diff.value.rows),
    count: versionRows(diff.value.rows).length,
  },
])

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

function close() {
  emit('update:open', false)
}

function doRestore() {
  if (!props.version) return
  emit('restore', props.version.id)
  close()
}

function onKeydown(e) {
  if (e.key === 'Escape') close()
}

watch(
  () => props.open,
  (v) => {
    if (v) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  },
)
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 120;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--overlay);
}
.dlg {
  width: min(1080px, 100%);
  height: min(820px, 90vh);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  box-shadow: 0 32px 80px -32px rgba(var(--shade-rgb), 0.9);
  overflow: hidden;
}
.dlg-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px 12px;
  border-bottom: 1px solid var(--line-soft);
}
.head-txt {
  flex: 1;
  min-width: 0;
}
.dlg-head h2 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 4px;
  font-family: theme('fontFamily.serif');
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}
.v-src {
  border: 1px solid rgba(var(--run-rgb), 0.38);
  border-radius: 999px;
  background: rgba(var(--run-rgb), 0.12);
  color: var(--run-fg);
  font-size: 11px;
  font-weight: 500;
  line-height: 1;
  padding: 4px 8px;
}
.dlg-sub {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-faint);
}
.dlg-sub em {
  font-family: theme('fontFamily.mono');
  font-style: normal;
}
.v-add {
  color: var(--info);
}
.v-del {
  color: var(--danger);
}
.v-run {
  margin-left: 6px;
  color: var(--text-muted);
  cursor: help;
}
.head-acts {
  display: flex;
  align-items: center;
  gap: 8px;
}
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: color 0.18s var(--ease), border-color 0.18s var(--ease);
}
.icon-btn:hover {
  color: var(--text);
  border-color: var(--line-strong);
}
.panes {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  min-height: 0;
}
.pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.pane + .pane {
  border-left: 1px solid var(--line-soft);
}
.pane-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  border-bottom: 1px solid var(--line-soft);
  background: var(--panel-2);
  font-size: 11.5px;
  color: var(--text-muted);
}
.pane-meta {
  margin-left: auto;
  font-family: theme('fontFamily.mono');
  color: var(--text-faint);
}
.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px 14px;
  background: var(--bg);
}
.ln {
  margin: 0;
  padding: 2px 6px;
  border-radius: 4px;
  font-family: theme('fontFamily.serif');
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
.ln.same {
  color: var(--text-faint);
}
.ln.add {
  border-left: 2px solid rgba(var(--info-rgb), 0.55);
  background: rgba(var(--info-rgb), 0.09);
}
.ln.del {
  border-left: 2px solid rgba(var(--danger-rgb), 0.5);
  background: rgba(var(--danger-rgb), 0.08);
  color: var(--text-muted);
}
.dlg-foot {
  padding: 9px 16px;
  border-top: 1px solid var(--line-soft);
}
.foot-tip {
  margin: 0;
  font-size: 11px;
  color: var(--text-faint);
}
</style>
