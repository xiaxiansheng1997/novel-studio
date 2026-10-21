<template>
  <div class="app-shell">
    <AppNav />
    <main id="main" class="live-main">
      <!-- 顶栏：标题 + 连接配置 -->
      <header class="live-header">
        <div class="title-block">
          <h1>实时浏览器</h1>
          <span class="pill" :class="connected ? 'pill--on' : 'pill--off'">
            <span class="status-dot" :class="connected ? 'on' : 'off'"></span>
            {{ connected ? '已连接' : '未连接' }}
          </span>
        </div>

        <div class="conn">
          <select v-model="provider" class="field provider-select" :disabled="connected" aria-label="Provider">
            <option value="qwen">通义千问</option>
            <option value="doubao">豆包</option>
            <option value="deepseek">DeepSeek</option>
          </select>
          <input
            v-model="url"
            class="field url-input"
            placeholder="目标地址（留空使用默认首页）"
            aria-label="目标地址"
          />
          <button class="btn btn-primary" :disabled="connected" @click="connect">
            <AppIcon name="play" :size="13" />
            连接
          </button>
          <button class="btn" :disabled="!connected" @click="disconnect">断开</button>
          <button
            class="btn"
            title="连同后台浏览器一起关掉（管理页的「浏览器」会熄灭）；下次聊天会重新拉起"
            @click="closeBrowser"
          >
            <AppIcon name="x" :size="13" />
            关闭浏览器
          </button>
        </div>
      </header>

      <p class="live-note">
        画面就是后台聊天用的<b>同一个标签页</b>：/v1 请求过程中打出的字、弹出的验证码都会直接出现在这里，
        在画面上完成验证后，后端会自动把那条消息重发一次。
      </p>

      <!-- 操作条 -->
      <div class="actionbar">
        <div class="toolbar">
          <button class="btn btn-sm" @click="sendCtrl('reload')" :disabled="!connected">
            <AppIcon name="refresh" :size="14" />
            刷新页面
          </button>
          <button class="btn btn-sm" :disabled="!connected" @click="autoSlide">
            <AppIcon name="zap" :size="14" />
            自动过滑块
          </button>
          <button class="btn btn-sm" :class="{ 'btn-danger': recording }" :disabled="!connected" @click="toggleRecord">
            <AppIcon name="stop" :size="12" v-if="recording" />
            <AppIcon name="activity" :size="14" v-else />
            {{ recording ? '停止录制并保存' : '开始录制接口' }}
          </button>
          <button class="btn btn-sm" :disabled="!connected" @click="clearCooldown">
            <AppIcon name="check" :size="14" />
            已验证 · 清除风控冷却
          </button>
        </div>

        <div class="type-bar">
          <AppIcon name="terminal" :size="15" class="type-icon" />
          <input
            v-model="bulkText"
            placeholder="辅助输入：直接在这里输入中文/长文本，回车或点发送自动发到页面"
            aria-label="辅助输入"
            @keydown.enter.prevent="sendBulk"
          />
          <label class="auto-send" :class="{ on: autoSend }" title="关闭后只把文本填进页面输入框，不自动发送">
            <input type="checkbox" v-model="autoSend" />
            自动发送
          </label>
          <button class="btn btn-sm" @click="sendBulk" :disabled="!connected || !bulkText">发送</button>
        </div>
      </div>

      <!-- 画面 -->
      <section class="stage-wrap">
        <div class="stage-chrome">
          <div class="stage-dots" aria-hidden="true">
            <span></span><span></span><span></span>
          </div>
          <span class="stage-host">{{ stageHost }}</span>
          <span class="stage-status">
            <span class="status-dot" :class="connected ? 'on' : 'off'"></span>
            {{ statusText }}
          </span>
        </div>
        <div class="live-stage" ref="stageEl" tabindex="0" @keydown="onKeyDown">
          <img
            v-if="frameSrc"
            :src="frameSrc"
            class="live-shot"
            alt="远程浏览器画面"
            @mousedown="onMouseDown"
            @mousemove="onMouseMove"
            @wheel.prevent="onWheel"
          />
          <div v-else class="live-placeholder">
            <div class="ph-icon"><AppIcon name="monitor" :size="26" /></div>
            <p>点击「连接」开始远程浏览器会话</p>
            <p class="ph-sub">登录、录制接口、处理人机验证都在这里完成</p>
          </div>
          <!-- 隐藏输入框：承载中文输入法的组合输入（普通 div 无法激活 IME） -->
          <input
            ref="imeEl"
            class="ime-input"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            @compositionstart="onCompositionStart"
            @compositionend="onCompositionEnd"
            @input="onImeInput"
            @paste="onPaste"
          />
        </div>
      </section>

      <!-- 日志 -->
      <section class="log-panel">
        <div class="log-head">
          <span class="log-title"><AppIcon name="terminal" :size="14" /> 事件日志</span>
          <span class="log-count">{{ logs.length }}</span>
          <span class="log-spacer"></span>
          <button class="btn btn-sm" @click="copyLogs" :disabled="!logs.length">复制</button>
          <button class="btn btn-sm" @click="logs.splice(0)" :disabled="!logs.length">清空</button>
        </div>
        <div class="live-log" ref="logEl">
          <div v-for="(line, i) in logs" :key="i" class="log-line" :class="line.kind">
            <span class="log-time">{{ line.time }}</span>
            <span class="log-text">{{ line.text }}</span>
          </div>
          <div v-if="!logs.length" class="log-empty">暂无事件</div>
        </div>
      </section>
    </main>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted, onUnmounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { getApiKey } from '../utils/storage'
import AppNav from '../components/AppNav.vue'
import AppIcon from '../components/AppIcon.vue'

const route = useRoute()
// 记住上次用的 provider 与“是否想保持连接”：SPA 路由切到聊天页会卸载本组件，
// 回来时自动恢复连接，不用再手点「连接」
const PROVIDER_KEY = 'w2a_live_provider'
const WANT_KEY = 'w2a_live_want'
function savedProvider() { try { return localStorage.getItem(PROVIDER_KEY) || '' } catch (_) { return '' } }
function wantReconnect() { try { return localStorage.getItem(WANT_KEY) === '1' } catch (_) { return false } }
const provider = ref(route.query.provider || savedProvider() || 'qwen')
const url = ref(route.query.url || '')
const connected = ref(false)
const statusText = ref('未连接')
const frameSrc = ref('')
const stageEl = ref(null)
const imeEl = ref(null)
const logEl = ref(null)
const recording = ref(false)
const logs = ref([])
const bulkText = ref('')
const autoSend = ref(true)
let ws = null
let composing = false
let logicalW = 1280, logicalH = 800, dsf = 1

const stageHost = computed(() => {
  if (url.value.trim()) return url.value.trim()
  return provider.value === 'qwen' ? 'chat.qwen.ai' : provider.value
})

function appendLog(msg, kind) {
  const text = String(msg ?? '')
  if (!kind) {
    if (/^(✓|已)/.test(text)) kind = 'ok'
    else if (/^(✗|错误|失败)|HTTP [45]\d\d/.test(text)) kind = 'err'
    else kind = 'info'
  }
  logs.value.push({ time: new Date().toLocaleTimeString('zh-CN', { hour12: false }), text, kind })
  if (logs.value.length > 400) logs.value.splice(0, logs.value.length - 400)
  nextTick(() => {
    if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
  })
}

async function copyLogs() {
  const text = logs.value.map(l => `[${l.time}] ${l.text}`).join('\n')
  try {
    await navigator.clipboard.writeText(text)
    appendLog('日志已复制到剪贴板')
  } catch (_) {
    appendLog('复制失败：剪贴板不可用')
  }
}

function toggleRecord() {
  if (!connected.value || !ws) return
  recording.value = !recording.value
  ws.send(JSON.stringify({ t: 'record', on: recording.value }))
  appendLog(recording.value ? '开始录制：请在画面中发一条对话（如：你好），完成后点「停止录制并保存」' : '正在停止录制…')
}

function connect() {
  if (ws) { ws.close(); ws = null }
  // 记录“用户想保持连接”：即使这次开浏览器失败，下次回到 /live 也自动重试
  try {
    localStorage.setItem(PROVIDER_KEY, provider.value)
    localStorage.setItem(WANT_KEY, '1')
  } catch (_) { /* 隐私模式下不可用，忽略 */ }
  const params = new URLSearchParams()
  if (url.value.trim()) params.set('url', url.value.trim())
  const apiKey = getApiKey()
  if (apiKey) params.set('token', apiKey)
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const qs = params.toString() ? '?' + params.toString() : ''
  ws = new WebSocket(`${proto}://${location.host}/ws/live/${provider.value}${qs}`)
  ws.onopen = () => {
    connected.value = true
    statusText.value = '已连接'
    appendLog('连接成功')
  }
  ws.onclose = () => { connected.value = false; statusText.value = '未连接'; frameSrc.value = ''; appendLog('连接已断开') }
  ws.onerror = () => { statusText.value = '连接错误'; appendLog('WebSocket 连接错误') }
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data)
    if (d.t === 'meta') { logicalW = d.w || 1280; logicalH = d.h || 800; dsf = d.dsf || 1 }
    else if (d.t === 'frame') { frameSrc.value = 'data:image/jpeg;base64,' + d.data }
    else if (d.t === 'log') { statusText.value = d.message; appendLog(d.message) }
    else if (d.t === 'error') { appendLog('错误：' + d.message, 'err') }
    else if (d.t === 'autoslide_result') {
      const r = d.result || {}
      appendLog((r.ok ? '✓ ' : '✗ ') + (r.message || '未知结果'))
    }
    else if (d.t === 'record_result') {
      const r = d.result
      if (!r) appendLog('录制结束：未录制到数据')
      else if (r.skipped) appendLog('录制结束：未抓到对话请求，已保留原 fixtures')
      else appendLog(`录制完成：识别到 ${r.count} 个请求，端点=${r.endpoint || '未识别'}，已写入 ${r.file}`)
      recording.value = false
    }
  }
}

function closeSocket() {
  if (ws) { ws.close(); ws = null }
  connected.value = false
  statusText.value = '未连接'
  frameSrc.value = ''
  recording.value = false
}

function disconnect() {
  // 用户主动断开：下次进来不要再自动连
  try { localStorage.setItem(WANT_KEY, '0') } catch (_) { /* 忽略 */ }
  closeSocket()
  appendLog('已断开画面：浏览器仍在后台运行（管理页的「浏览器」仍亮），下次连接会秒开；要彻底关掉请点「关闭浏览器」')
}

async function closeBrowser() {
  // 断开只停推屏，后台浏览器/标签页还在（后台聊天要复用，也能避免反复重启）。
  // 要彻底关掉走 full=1：管理页的「浏览器」状态才会变灰。
  try { localStorage.setItem(WANT_KEY, '0') } catch (_) { /* 忽略 */ }
  closeSocket()
  const headers = {}
  const apiKey = getApiKey()
  if (apiKey) headers['Authorization'] = 'Bearer ' + apiKey
  try {
    const r = await fetch(`/live/close/${provider.value}?full=true`, { method: 'POST', headers })
    appendLog(r.ok
      ? '已关闭浏览器进程（管理页的「浏览器」会熄灭），下次聊天会重新拉起'
      : '关闭浏览器失败：HTTP ' + r.status)
  } catch (e) {
    appendLog('关闭浏览器失败：' + e)
  }
}

function sendBulk() {
  const text = bulkText.value.trim()
  if (!connected.value || !ws || !text) return
  if (autoSend.value) {
    // 交给后端 trigger_send：清空输入框 -> 聚焦 -> 输入 -> 回车（不生效则点发送按钮）
    ws.send(JSON.stringify({ t: 'send', value: text }))
    appendLog(`发送中：${text.slice(0, 40)}`)
  } else {
    // 只填进页面输入框，发送动作由用户在画面里自己完成
    sendText(text)
    appendLog('已填入页面输入框（自动发送已关闭）')
  }
  bulkText.value = ''
}

function onPaste(e) {
  e.preventDefault()
  const text = (e.clipboardData || window.clipboardData).getData('text')
  if (text) sendText(text)
}

function sendCtrl(action) { if (ws) ws.send(JSON.stringify({ t: action })) }

function autoSlide() {
  if (!connected.value || !ws) return
  appendLog('开始自动拖动滑块…')
  ws.send(JSON.stringify({ t: 'autoslide' }))
}

async function clearCooldown() {
  const headers = {}
  const apiKey = getApiKey()
  if (apiKey) headers['Authorization'] = 'Bearer ' + apiKey
  try {
    const r = await fetch(`/live/cooldown/clear/${provider.value}`, { method: 'POST', headers })
    appendLog(r.ok ? '已清除风控冷却，可以立即重试聊天' : '清除冷却失败：HTTP ' + r.status)
  } catch (e) {
    appendLog('清除冷却失败：' + e)
  }
}

function toLogical(e) {
  const el = stageEl.value?.querySelector('img')
  if (!el) return { x: 0, y: 0 }
  const r = el.getBoundingClientRect()
  let fx = (e.clientX - r.left) / r.width
  let fy = (e.clientY - r.top) / r.height
  fx = Math.max(0, Math.min(1, fx))
  fy = Math.max(0, Math.min(1, fy))
  return { x: fx * (logicalW / dsf), y: fy * (logicalH / dsf) }
}

function onMouseDown(e) {
  e.preventDefault()
  imeEl.value?.focus() // 焦点给它，中文输入法才能正常工作
  dragging = true
  const p = toLogical(e)
  if (ws) ws.send(JSON.stringify({ t: 'mouse', action: 'down', x: p.x, y: p.y, button: 'left', clickCount: 1 }))
}

let dragging = false
function onMouseUp(e) {
  if (!dragging) return
  dragging = false
  const p = toLogical(e)
  if (ws) ws.send(JSON.stringify({ t: 'mouse', action: 'up', x: p.x, y: p.y, button: 'left', clickCount: 1 }))
}

let lastMove = 0
function onMouseMove(e) {
  const now = Date.now()
  if (now - lastMove < (dragging ? 16 : 50)) return // 拖拽时高频上报，保证滑块轨迹连续
  lastMove = now
  const p = toLogical(e)
  if (!ws) return
  const msg = { t: 'mouse', action: 'move', x: p.x, y: p.y }
  if (dragging) {
    // 关键：拖拽时必须标记左键按住，否则 CDP 不认为是拖动，滑块不跟手
    msg.button = 'left'
    msg.clickCount = 1
  }
  ws.send(JSON.stringify(msg))
}

function onWheel(e) {
  const p = toLogical(e)
  if (ws) ws.send(JSON.stringify({ t: 'wheel', x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY }))
}

// ---- 文本输入：统一走隐藏 input（英文/数字/中文输入法上屏都能覆盖）----
function sendText(text) {
  if (!connected.value || !ws || !text) return
  ws.send(JSON.stringify({ t: 'text', value: text }))
}

function onCompositionStart() { composing = true }
function onCompositionEnd(e) {
  composing = false
  const el = imeEl.value
  const text = (el && el.value) || e.data || ''
  sendText(text)
  if (el) el.value = ''
}

function onImeInput(e) {
  if (e.isComposing || composing) return // 组合中，等 compositionend 上屏
  const el = e.target
  const text = el.value
  sendText(text)
  el.value = ''
}

const SPECIAL_KEYS = new Set([
  'Enter', 'Backspace', 'Delete', 'Tab', 'Escape',
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'Home', 'End', 'PageUp', 'PageDown',
])

// 键盘：可打印字符交给 input 事件，这里只处理特殊键与组合快捷键
function onKeyDown(e) {
  if (!connected.value || !ws) return
  if (composing || e.isComposing || e.keyCode === 229) return // 输入法组合阶段不干预
  const printable = e.key.length === 1
  const isShortcut = e.ctrlKey || e.metaKey || e.altKey
  if (printable && !isShortcut) return // 普通字符由 input 事件处理
  if (!printable && !SPECIAL_KEYS.has(e.key) && !isShortcut) return
  const mods = { alt: e.altKey, ctrl: e.ctrlKey, meta: e.metaKey, shift: e.shiftKey }
  ws.send(JSON.stringify({ t: 'key', etype: 'keyDown', key: e.key, code: e.code, mods }))
  ws.send(JSON.stringify({ t: 'key', etype: 'keyUp', key: e.key, code: e.code, mods }))
  e.preventDefault()
}

onMounted(() => {
  window.addEventListener('mouseup', onMouseUp)
  // 带 ?provider= 进来（管理页跳转）或上次会话还开着：直接恢复连接
  if (route.query.provider || wantReconnect()) {
    appendLog('自动恢复上次连接：' + provider.value)
    connect()
  }
})
onUnmounted(() => { window.removeEventListener('mouseup', onMouseUp); closeSocket() })
</script>

<style scoped>
.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: #070a10;
}
.live-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.live-note {
  margin: 0;
  padding: 7px 20px;
  border-bottom: 1px solid var(--line-soft);
  background: #070b12;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-faint);
}
.live-note b {
  color: var(--text-muted);
  font-weight: 600;
}

/* 顶栏 */
.live-header {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  padding: 12px 20px;
  border-bottom: 1px solid var(--line-soft);
  background: rgba(10, 14, 22, 0.9);
}
.title-block {
  display: flex;
  align-items: center;
  gap: 10px;
}
.title-block h1 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.conn {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 320px;
  justify-content: flex-end;
}
.provider-select {
  width: 128px;
  flex-shrink: 0;
}
.url-input {
  flex: 1;
  max-width: 440px;
  min-width: 160px;
}

/* 操作条 */
.actionbar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 10px 20px;
  border-bottom: 1px solid var(--line-soft);
  background: #080c13;
}
.type-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 300px;
}
.type-icon {
  color: var(--text-faint);
  flex-shrink: 0;
}
.type-bar input {
  flex: 1;
  height: 30px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid var(--line-soft);
  background: #0d131d;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
}
.type-bar input::placeholder {
  color: var(--text-faint);
}
.type-bar input:focus {
  border-color: var(--line-strong);
  box-shadow: var(--ring);
}
.auto-send {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  font-size: 11.5px;
  color: var(--text-faint);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
.auto-send input {
  width: 13px;
  height: 13px;
  margin: 0;
  accent-color: #22c55e;
  cursor: pointer;
}
.auto-send.on {
  color: var(--text-muted);
}

/* 画面 */
.stage-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 14px 20px 0;
}
.stage-chrome {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 4px 8px;
  font-size: 11px;
  color: var(--text-faint);
}
.stage-dots {
  display: flex;
  gap: 5px;
}
.stage-dots span {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #223044;
}
.stage-dots span:first-child { background: #3b4453; }
.stage-host {
  font-family: theme('fontFamily.mono');
  color: var(--text-muted);
}
.stage-status {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.live-stage {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border-radius: 16px;
  border: 1px solid var(--line);
  background-color: #05080d;
  background-image: radial-gradient(rgba(34, 197, 94, 0.08) 1px, transparent 1px);
  background-size: 22px 22px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03), 0 30px 60px -40px rgba(0, 0, 0, 0.9);
}
.live-shot {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  cursor: crosshair;
}
.live-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  color: var(--text-faint);
  text-align: center;
}
.ph-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 54px;
  height: 54px;
  margin-bottom: 8px;
  border-radius: 15px;
  border: 1px solid var(--line);
  background: #0b1018;
  color: var(--text-muted);
}
.live-placeholder p {
  margin: 0;
  font-size: 13.5px;
  color: var(--text-muted);
}
.live-placeholder .ph-sub {
  font-size: 12px;
  color: var(--text-faint);
}
.ime-input {
  /* 必须位于可视区域内：IME 候选词窗口跟随输入框位置，放屏幕外会导致候选框看不到 */
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 1px;
  height: 1px;
  opacity: 0;
  border: none;
  outline: none;
  background: transparent;
  pointer-events: none;
}

/* 日志 */
.log-panel {
  flex-shrink: 0;
  margin: 12px 20px 14px;
  border-radius: 12px;
  border: 1px solid var(--line-soft);
  background: #080c13;
  overflow: hidden;
}
.log-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px 7px 12px;
  border-bottom: 1px solid var(--line-soft);
  color: var(--text-muted);
  font-size: 11.5px;
}
.log-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.log-count {
  font-family: theme('fontFamily.mono');
  color: var(--text-faint);
}
.log-spacer {
  flex: 1;
}
.live-log {
  max-height: 132px;
  overflow-y: auto;
  padding: 8px 12px;
  font-family: theme('fontFamily.mono');
  font-size: 11.5px;
  line-height: 1.7;
}
.log-line {
  display: flex;
  gap: 10px;
}
.log-time {
  color: #46566b;
  flex-shrink: 0;
}
.log-text {
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}
.log-line.ok .log-text { color: #86efac; }
.log-line.err .log-text { color: #fca5a5; }
.log-empty {
  color: var(--text-faint);
  padding: 4px 0;
}

@media (max-width: 900px) {
  .live-header {
    padding: 10px 12px;
  }
  .conn {
    justify-content: flex-start;
    min-width: 0;
  }
  .url-input {
    max-width: none;
  }
  .actionbar {
    padding: 8px 12px;
  }
  .stage-wrap {
    padding: 10px 12px 0;
  }
  .log-panel {
    margin: 10px 12px 12px;
  }
}
@media (max-width: 640px) {
  /* 窄屏：标题独占一行，URL 独占一行，provider 与连接按钮同排，避免被挤出屏幕 */
  .live-header {
    gap: 8px;
  }
  .title-block {
    flex: 1 0 100%;
  }
  .conn {
    width: 100%;
    flex-wrap: wrap;
    gap: 6px;
  }
  .provider-select {
    width: auto;
    flex: 1 1 110px;
  }
  .url-input {
    order: -1;
    flex: 1 1 100%;
    max-width: none;
  }
}
</style>
