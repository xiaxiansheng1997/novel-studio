<template>
  <div class="app-shell">
    <AppNav />
    <main id="main" class="admin-main">
      <header class="admin-header">
        <div class="title-block">
          <h1>管理面板</h1>
          <p class="subtitle">Provider 状态、接口调用与快捷操作</p>
        </div>
        <div class="header-actions">
          <span v-if="reloadMsg" class="reload-msg">{{ reloadMsg }}</span>
          <button class="btn btn-sm" :disabled="loading" @click="refreshAll">
            <AppIcon name="refresh" :size="14" />
            {{ loading ? '刷新中…' : '刷新' }}
          </button>
        </div>
      </header>

      <!-- KPI -->
      <section class="kpis" aria-label="关键指标">
        <div class="kpi card">
          <span class="kpi-label"><AppIcon name="activity" :size="14" /> 请求总数</span>
          <span class="kpi-value">{{ stats.total ?? '—' }}</span>
          <span class="kpi-foot">最近 500 条环形缓冲</span>
        </div>
        <div class="kpi card">
          <span class="kpi-label"><AppIcon name="zap" :size="14" /> 平均耗时</span>
          <span class="kpi-value">{{ stats.avg_ms != null ? stats.avg_ms + ' ms' : '—' }}</span>
          <span class="kpi-foot">仅统计已完成的请求</span>
        </div>
        <div class="kpi card">
          <span class="kpi-label"><AppIcon name="alert" :size="14" /> 错误率</span>
          <span class="kpi-value" :class="{ danger: stats.error_rate > 5 }">{{ stats.error_rate ?? 0 }}%</span>
          <span class="kpi-foot">HTTP ≥ 400 占比</span>
        </div>
        <div class="kpi card">
          <span class="kpi-label"><AppIcon name="cpu" :size="14" /> 可用模型</span>
          <span class="kpi-value">{{ totalModels }}</span>
          <span class="kpi-foot">{{ providers.length }} 个 Provider</span>
        </div>
      </section>

      <div class="admin-grid">
        <!-- Provider 状态 -->
        <section class="card">
          <div class="card-head">
            <span class="card-title">Provider 状态</span>
            <span class="card-sub">{{ readyCount }}/{{ providers.length }} 就绪</span>
            <span class="head-spacer"></span>
            <router-link to="/live" class="btn btn-sm">打开内置浏览器</router-link>
          </div>
          <div class="card-body">
            <div class="provider-cards">
              <article v-for="p in providers" :key="p.provider" class="provider-card">
                <div class="pc-head">
                  <div class="pc-name-wrap">
                    <span class="pc-name">{{ p.display_name || p.provider }}</span>
                    <span class="pc-key">{{ p.provider }}</span>
                  </div>
                  <span class="pill" :class="p.has_profile ? 'pill--on' : 'pill--warn'">
                    {{ p.has_profile ? '已登录' : '待登录' }}
                  </span>
                </div>

                <div class="pc-status">
                  <span class="pc-status-item">
                    <span class="status-dot" :class="p.registered ? 'on' : 'off'"></span>
                    已注册
                  </span>
                  <span class="pc-status-item">
                    <span class="status-dot" :class="p.has_profile ? 'on' : 'off'"></span>
                    Profile
                  </span>
                  <span class="pc-status-item">
                    <span class="status-dot" :class="p.browser_open ? 'on' : 'off'"></span>
                    浏览器
                  </span>
                </div>

                <div class="pc-models">
                  <span v-for="m in modelTags(p)" :key="m.id" class="tag" :title="m.id">{{ m.label }}</span>
                  <span v-if="!modelTags(p).length" class="pc-no-model">暂无模型</span>
                </div>

                <div class="pc-caps">
                  <span class="pc-caps-key">模式</span>
                  <span v-for="mo in capsOf(p).modes" :key="mo.id" class="tag tag--mode">{{ mo.label }}</span>
                  <span v-if="!capsOf(p).modes.length" class="pc-no-model">—</span>
                </div>

                <div class="pc-align" :class="alignClass(p)">
                  <span class="pc-caps-key">最近对齐</span>
                  <span>{{ alignText(p) }}</span>
                </div>

                <router-link :to="{ path: '/live', query: { provider: p.provider } }" class="pc-link">
                  打开 {{ p.provider }} 浏览器
                  <AppIcon name="chevronRight" :size="13" />
                </router-link>
              </article>
              <div v-if="!providers.length" class="empty-block">
                <AppIcon name="cpu" :size="18" />
                <span>暂无 Provider，检查 config.yaml</span>
              </div>
            </div>
          </div>
        </section>

        <!-- 快捷操作 -->
        <section class="card">
          <div class="card-head">
            <span class="card-title">快捷操作</span>
            <span class="card-sub">常用维护入口</span>
          </div>
          <div class="card-body actions">
            <router-link to="/live" class="action-row">
              <span class="action-icon"><AppIcon name="monitor" :size="16" /></span>
              <span class="action-text">
                <strong>内嵌浏览器</strong>
                <small>登录站点、录制接口、处理人机验证</small>
              </span>
              <AppIcon name="chevronRight" :size="15" class="action-arrow" />
            </router-link>
            <button class="action-row" @click="reloadConfig">
              <span class="action-icon"><AppIcon name="refresh" :size="16" /></span>
              <span class="action-text">
                <strong>重载配置</strong>
                <small>不重启进程，重新读取 config.yaml 与模型列表</small>
              </span>
              <AppIcon name="chevronRight" :size="15" class="action-arrow" />
            </button>
            <div class="action-row is-static">
              <span class="action-icon"><AppIcon name="key" :size="16" /></span>
              <span class="action-text">
                <strong>OpenAI 兼容端点</strong>
                <small class="mono">POST /v1/chat/completions · GET /v1/models</small>
              </span>
            </div>
          </div>
        </section>

        <!-- 最近请求 -->
        <section class="card">
          <div class="card-head">
            <span class="card-title">最近请求</span>
            <span class="card-sub">{{ logs.length }} 条</span>
            <span class="head-spacer"></span>
            <span v-if="logsError" class="log-error">{{ logsError }}</span>
          </div>
          <div class="table-wrap">
            <table class="req-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>方法</th>
                  <th>路径</th>
                  <th class="col-model">模型</th>
                  <th>状态</th>
                  <th class="col-ms">耗时</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(l, i) in logs" :key="i">
                  <td class="mono">{{ fmtTime(l.ts) }}</td>
                  <td class="mono">{{ l.method }}</td>
                  <td class="mono path-cell">{{ l.path }}</td>
                  <td class="mono col-model">{{ l.model || '—' }}</td>
                  <td>
                    <span class="status-code" :class="l.status >= 400 ? 'bad' : 'good'">{{ l.status }}</span>
                  </td>
                  <td class="mono col-ms">{{ l.duration_ms ? Math.round(l.duration_ms) + ' ms' : '—' }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="!logs.length" class="empty-block">
              <AppIcon name="activity" :size="18" />
              <span>还没有 API 请求记录，调用一次 /v1/chat/completions 后出现</span>
            </div>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useProviderStore } from '../stores/provider'
import AppNav from '../components/AppNav.vue'
import AppIcon from '../components/AppIcon.vue'

const providerStore = useProviderStore()
const providers = ref([])
const stats = ref({ total: 0, avg_ms: 0, error_rate: 0, by_provider: {} })
const logs = ref([])
const logsError = ref('')
const loading = ref(false)
const reloadMsg = ref('')

const readyCount = computed(() => providers.value.filter(p => p.has_profile).length)
const totalModels = computed(() => providers.value.reduce((n, p) => n + (p.models?.length || 0), 0))

onMounted(() => refreshAll())

async function refreshAll() {
  loading.value = true
  try {
    await Promise.allSettled([loadStatus(), loadStats(), loadLogs(), providerStore.fetchModels()])
  } finally {
    loading.value = false
  }
}

async function loadStatus() {
  await providerStore.fetchStatus()
  providers.value = providerStore.status
}

async function loadStats() {
  try {
    const r = await fetch('/admin/stats')
    if (r.ok) stats.value = await r.json()
  } catch (_) {
    /* 统计不可用时保留旧值 */
  }
}

async function loadLogs() {
  logsError.value = ''
  try {
    const r = await fetch('/admin/logs?limit=20')
    if (!r.ok) throw new Error('HTTP ' + r.status)
    logs.value = await r.json()
  } catch (e) {
    logs.value = []
    logsError.value = '日志不可用：' + e.message
  }
}

async function reloadConfig() {
  reloadMsg.value = '正在重载…'
  try {
    await fetch('/admin/reload')
    await Promise.allSettled([loadStatus(), providerStore.fetchModels()])
    reloadMsg.value = '配置已重载'
  } catch (e) {
    reloadMsg.value = '重载失败：' + e.message
  }
  setTimeout(() => (reloadMsg.value = ''), 3000)
}

function fmtTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

// ---- 能力行：模型文案 / 模式 / 最近一次页面模式对齐结果 ----
function capsOf(p) {
  return providerStore.providers.find(c => c.key === p.provider) || { models: [], modes: [], features: [] }
}

function modelTags(p) {
  const caps = capsOf(p)
  if (caps.models && caps.models.length) return caps.models
  return (p.models || []).map(id => ({ id, label: id }))
}

function alignText(p) {
  const st = capsOf(p).last_mode_status
  if (!st) return '暂无（还没发过带模式的请求）'
  if (st.ok) return `已对齐 ${st.preset || ''} ${st.ms != null ? st.ms + 'ms' : ''}`.trim()
  const miss = (st.missing || []).join('/') || '未知'
  return `未对齐：${miss}（去 /live 探测文案）`
}

function alignClass(p) {
  const st = capsOf(p).last_mode_status
  if (!st) return ''
  return st.ok ? 'is-ok' : 'is-bad'
}
</script>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: radial-gradient(1000px 500px at 80% -10%, rgba(34, 197, 94, 0.05), transparent 60%), #070a10;
}
.admin-main {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 22px 26px 32px;
}

/* 头部 */
.admin-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  max-width: 1240px;
  margin: 0 auto 20px;
}
.title-block h1 {
  margin: 0 0 2px;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.02em;
}
.subtitle {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-faint);
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.reload-msg {
  font-size: 12px;
  color: #86efac;
}

/* KPI */
.kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  max-width: 1240px;
  margin: 0 auto 14px;
}
.kpi {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 16px;
}
.kpi-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-faint);
}
.kpi-value {
  font-family: theme('fontFamily.mono');
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}
.kpi-value.danger {
  color: #fca5a5;
}
.kpi-foot {
  font-size: 11px;
  color: var(--text-faint);
}

/* 主网格 */
.admin-grid {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 1240px;
  margin: 0 auto;
}
.head-spacer {
  flex: 1;
}
.card-body {
  padding: 16px;
}
.provider-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.provider-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-radius: 12px;
  border: 1px solid var(--line-soft);
  background: #0c121b;
  transition: border-color 0.18s var(--ease);
}
.provider-card:hover {
  border-color: var(--line);
}
.pc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.pc-name-wrap {
  display: flex;
  flex-direction: column;
}
.pc-name {
  font-size: 13.5px;
  font-weight: 600;
}
.pc-key {
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-faint);
}
.pc-status {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
.pc-status-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--text-muted);
}
.pc-models {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.pc-no-model {
  font-size: 11px;
  color: var(--text-faint);
}
.pc-caps {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.pc-caps-key {
  font-size: 11px;
  color: var(--text-faint);
  margin-right: 2px;
}
.tag--mode {
  color: var(--text-muted);
  border-style: dashed;
}
.pc-align {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 11px;
  color: var(--text-faint);
  min-height: 16px;
}
.pc-align.is-ok {
  color: #86efac;
}
.pc-align.is-bad {
  color: #fca5a5;
}
.pc-link {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-top: auto;
  padding-top: 2px;
  font-size: 11.5px;
  color: var(--text-faint);
  text-decoration: none;
  transition: color 0.16s var(--ease);
}
.pc-link:hover {
  color: var(--run);
}

/* 快捷操作 */
.actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.action-row {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 11px 12px;
  border-radius: 11px;
  border: 1px solid var(--line-soft);
  background: #0c121b;
  text-align: left;
  text-decoration: none;
  color: var(--text);
  cursor: pointer;
  transition: border-color 0.18s var(--ease), background-color 0.18s var(--ease);
}
.action-row:hover {
  border-color: var(--line-strong);
  background: #0f1621;
}
.action-row.is-static {
  cursor: default;
}
.action-row.is-static:hover {
  border-color: var(--line-soft);
  background: #0c121b;
}
.action-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  border: 1px solid var(--line-soft);
  background: #0a0f17;
  color: var(--text-muted);
  flex-shrink: 0;
}
.action-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.action-text strong {
  font-size: 13px;
  font-weight: 600;
}
.action-text small {
  font-size: 11.5px;
  color: var(--text-faint);
}
.action-text small.mono {
  font-family: theme('fontFamily.mono');
}
.action-arrow {
  margin-left: auto;
  color: var(--text-faint);
}

/* 请求表 */
.table-wrap {
  overflow-x: auto;
}
.req-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.req-table th {
  padding: 8px 14px;
  text-align: left;
  font-weight: 500;
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-faint);
  border-bottom: 1px solid var(--line-soft);
  background: #0a0f17;
}
.req-table td {
  padding: 8px 14px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-muted);
  white-space: nowrap;
}
.req-table tr:last-child td {
  border-bottom: none;
}
.req-table tbody tr:hover {
  background: #0c121b;
}
.mono {
  font-family: theme('fontFamily.mono');
  font-size: 11.5px;
}
.path-cell {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.status-code {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: 6px;
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  border: 1px solid;
}
.status-code.good {
  color: #86efac;
  border-color: rgba(34, 197, 94, 0.3);
  background: rgba(34, 197, 94, 0.08);
}
.status-code.bad {
  color: #fca5a5;
  border-color: rgba(248, 113, 113, 0.3);
  background: rgba(248, 113, 113, 0.08);
}
.empty-block {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 22px 16px;
  color: var(--text-faint);
  font-size: 12px;
}
.log-error {
  font-size: 11.5px;
  color: #fca5a5;
}

@media (max-width: 1100px) {
  .kpis {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 720px) {
  .admin-main {
    padding: 16px 12px 24px;
  }
  .kpis {
    gap: 8px;
  }
  .kpi {
    padding: 12px;
  }
  .kpi-value {
    font-size: 20px;
  }
  .col-model {
    display: none;
  }
  .card-body {
    padding: 12px;
  }
  .col-ms {
    display: none;
  }
}
</style>
