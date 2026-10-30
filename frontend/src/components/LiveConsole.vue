<template>
  <div class="live-console">
    <!-- 常驻在控制台「浏览器」tab 里：不再自带 AppNav/app-shell，也不用 <main>
         （外层 ConsoleView 已经有一个） -->
    <div class="live-main">
      <!-- 顶栏：连接配置 -->
      <header class="live-header">
        <div class="title-block">
          <h1>实时画面</h1>
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
            title="连同后台浏览器一起关掉（控制台「状态」里的浏览器会熄灭）；下次聊天会重新拉起"
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

      <!-- 页面模式：校准网页端的「深度思考 / 联网 / 模型下拉」文案 -->
      <section class="mode-panel">
        <div class="mode-head">
          <span class="mode-title"><AppIcon name="sliders" :size="14" /> 页面模式</span>
          <span class="mode-hint">
            探测到的控件就是能命中的文案；带开/关状态的场景（如代码/翻译）可在下方加成自定义模式
          </span>
          <span class="mode-spacer"></span>
          <select v-model="modePreset" class="field mode-select" aria-label="模式预设" :disabled="!connected">
            <option v-for="m in modeOptions" :key="m.id" :value="m.id">{{ m.label }}</option>
          </select>
          <button class="btn btn-sm" :disabled="!connected || probing" @click="probeModes">
            <AppIcon name="search" :size="13" />
            {{ probing ? '探测中…' : '探测页面模式' }}
          </button>
          <button
            class="btn btn-sm"
            title="重读 config.yaml 并就地生效，不用重启后端"
            @click="reloadConfig"
            :disabled="reloading"
          >
            <AppIcon name="refresh" :size="13" />
            {{ reloading ? '重载中…' : '重载配置' }}
          </button>
          <button class="btn btn-sm" :disabled="!connected || applying" @click="applyPreset">
            <AppIcon name="check" :size="13" />
            {{ applying ? '应用中…' : '应用模式' }}
          </button>
        </div>
        <div v-if="probeItems.length" class="probe-strip">
          <span
            v-for="(it, i) in probeItems"
            :key="i"
            class="probe-chip"
            :class="{ on: probeOn(it), used: chipUsed(it.text) }"
            :title="chipTitle(it)"
            @click="openChipMenu(it, $event)"
          >
            {{ it.text }}
            <b v-if="probeState(it)">{{ probeState(it) }}</b>
          </span>
        </div>
        <!-- 绑定：常用的只露「模型触发器 + 各模型文案」，思考/联网/场景收进高级；存 storage/ui-bindings.yaml，不改 config.yaml -->
        <div v-if="bindingLoaded" class="bind-row">
          <span class="bind-title" :title="bindingsFile">绑定</span>
          <label class="bind-field">
            模型触发器
            <input v-model="bindModelTrigger" class="field bind-input" placeholder="下拉按钮当前显示的字，可留空" />
          </label>
          <label v-for="m in modelLabels" :key="m.id" class="bind-field">
            {{ m.id }}
            <input v-model="m.label" class="field bind-input" placeholder="下拉里的文案，留空=不切" />
          </label>
          <span v-if="advSummary" class="bind-sum" :title="'点「高级」修改：' + advSummary">{{ advSummary }}</span>
          <button class="btn btn-sm" :title="showAdvanced ? '收起思考/联网/场景绑定' : '展开思考/联网/场景绑定'" @click="showAdvanced = !showAdvanced">
            {{ showAdvanced ? '收起高级' : '高级（思考/联网/场景）' }}
          </button>
          <button class="btn btn-sm" @click="saveBindings" :disabled="savingBindings">
            <AppIcon name="check" :size="12" />
            {{ savingBindings ? '保存中…' : '保存绑定' }}
          </button>
          <button class="btn btn-sm" @click="clearBindings" :disabled="savingBindings" title="删除页面覆盖，回到 config.yaml 的写法">
            清除
          </button>
        </div>
        <div v-if="bindingLoaded && showAdvanced" class="bind-row bind-adv">
          <label class="bind-field">
            思考
            <select v-model="bindThinking" class="field bind-select">
              <option value="">未绑定</option>
              <option v-for="t in probeTexts" :key="'bt-' + t" :value="t">{{ t }}</option>
            </select>
          </label>
          <label class="bind-field">
            联网
            <select v-model="bindSearch" class="field bind-select">
              <option value="">未绑定</option>
              <option v-for="t in probeTexts" :key="'bs-' + t" :value="t">{{ t }}</option>
            </select>
          </label>
          <span class="bind-title">场景</span>
          <select v-model="customPick" class="field bind-select">
            <option value="">选探测到的开关</option>
            <option v-for="t in customCandidates" :key="'bc-' + t" :value="t">{{ t }}</option>
          </select>
          <input
            v-model="customKey"
            class="field bind-input bind-key"
            placeholder="英文键名，如 code"
            @keydown.enter.prevent="addCustom"
          />
          <button class="btn btn-sm" @click="addCustom" :disabled="!customPick || !customKey">添加</button>
          <span v-for="c in customFeatures" :key="'cf-' + c.key" class="bind-tag" :title="'调用：mode=' + c.key">
            {{ c.text }} <b>{{ c.key }}</b>
            <button class="bind-x" @click="removeCustom(c.key)" title="移除">×</button>
          </span>
        </div>
        <!-- 绑定体检：把“看着就绑错了”的地方直接说出来，而不是等日志刷「按钮未找到」 -->
        <p v-if="bindingLoaded && bindNotes.length" class="bind-notes" :class="{ 'is-bad': bindBad }">
          <span v-for="(n, i) in bindNotes" :key="'bn-' + i">{{ (i ? ' · ' : '') + n }}</span>
        </p>
      </section>

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
    </div>

    <!-- 点探测到的 chip = 直接把它绑给某个语义，省掉「先记住文案再去下拉里找」这一步 -->
    <Teleport to="body">
      <div v-if="chipMenu" class="chip-mask" @mousedown.self="chipMenu = null">
        <div class="chip-pop" :style="{ left: chipMenu.x + 'px', top: chipMenu.y + 'px' }">
          <p class="chip-pop-t">把「{{ chipMenu.text }}」绑给：</p>
          <button v-for="o in chipTargets" :key="o.key" type="button" class="chip-pop-i" @click="applyChip(o.key)">
            {{ o.label }}<em v-if="o.hint">{{ o.hint }}</em>
          </button>
          <button type="button" class="chip-pop-i chip-pop-x" @click="chipMenu = null">取消</button>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted, onUnmounted, computed, watch } from 'vue'
import { getApiKey } from '../utils/storage'
import { authHeaders } from '../utils/api'
import AppIcon from './AppIcon.vue'

// 控制台「浏览器」tab 的常驻内容。ConsoleView 用 v-show 保住本组件不销毁，
// active=false 只停推屏，切回来再接上（见文件底部那个 watch）。
const props = defineProps({
  provider: { type: String, default: '' },
  initialUrl: { type: String, default: '' },
  // 打开就自动连：老链接 /live?provider=x、Provider 卡片点进来都属于这种
  autoConnect: { type: Boolean, default: false },
  // 所在 tab 是否可见
  active: { type: Boolean, default: true },
})
const emit = defineEmits(['update:provider', 'update:connected'])

// 记住上次用的 provider 与“是否想保持连接”：切走/刷新后回来自动恢复，不用再手点「连接」
const PROVIDER_KEY = 'w2a_live_provider'
const WANT_KEY = 'w2a_live_want'
function savedProvider() { try { return localStorage.getItem(PROVIDER_KEY) || '' } catch (_) { return '' } }
function wantReconnect() { try { return localStorage.getItem(WANT_KEY) === '1' } catch (_) { return false } }
const provider = ref(props.provider || savedProvider() || 'qwen')
const url = ref(props.initialUrl || '')
watch(() => props.provider, (v) => {
  // 从外面（URL query / Provider 卡片）改过来了：没连着才跟随，别把正在推屏的会话换掉
  if (!v || v === provider.value || connected.value) return
  provider.value = v
})
watch(provider, (v) => {
  try { localStorage.setItem(PROVIDER_KEY, v) } catch (_) { /* 隐私模式下不可用，忽略 */ }
  emit('update:provider', v)
})
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
const modePreset = ref('quick')
const probing = ref(false)
const applying = ref(false)
const reloading = ref(false)
const probeItems = ref([])
const bindingLoaded = ref(false)
const savingBindings = ref(false)
const bindingsFile = ref('')
const bindThinking = ref('')
const bindSearch = ref('')
const bindModelTrigger = ref('')
const modelLabels = ref([])
// 服务端可能给同一控件配了多个文案（站点 A/B），保存时只改首选、其余保留；
// active_class / blind 面板不编辑，但必须原样带回，否则保存一次就被抹掉。
const uiPools = ref({ thinking: [], search: [], model: [] })
const uiExtras = ref({ thinking: {}, search: {}, model: {} })
const bindingModes = ref([])
const customFeatures = ref([])
const customPick = ref('')
const customKey = ref('')
// 思考/联网/场景不配就不该占地方，默认折起来；一旦里面已有绑定就自动展开
const showAdvanced = ref(false)
const STD_MODES = [
  { id: 'quick', label: '快速模式' },
  { id: 'think', label: '深度思考' },
  { id: 'search', label: '联网搜索' },
  { id: 'expert', label: '专家模式（思考 + 联网）' },
]
const STD_FEATURES = ['thinking', 'search', 'model']
const RESERVED_KEYS = ['thinking', 'search', 'model', 'quick', 'think', 'expert']
const probeTexts = computed(() => {
  const out = []
  for (const it of probeItems.value) {
    if (it.text && !out.includes(it.text)) out.push(it.text)
  }
  for (const v of [bindThinking.value, bindSearch.value, bindModelTrigger.value]) {
    if (v && !out.includes(v)) out.push(v)
  }
  for (const c of customFeatures.value) {
    if (c.text && !out.includes(c.text)) out.push(c.text)
  }
  return out
})
const customCandidates = computed(() => {
  const used = new Set(
    [bindThinking.value, bindSearch.value, bindModelTrigger.value, ...customFeatures.value.map((c) => c.text)].filter(Boolean)
  )
  const out = []
  for (const it of probeItems.value) {
    if (!it.text || used.has(it.text)) continue
    // 只收带 aria 状态的控件：自定义场景在 quick 里要能被关掉，盲点会每轮报 missing
    if (it.pressed == null && it.checked == null) continue
    if (!out.includes(it.text)) out.push(it.text)
  }
  return out
})

// ---- 绑定体检：把“一眼就不对”的组合当场说出来 ----
// 只有带 aria 开/关状态的控件才可能当开关用（盲点读不出状态，会每轮报 missing）
const toggleCandidates = computed(() => {
  const out = []
  for (const it of probeItems.value) {
    if (!it.text || (it.pressed == null && it.checked == null)) continue
    if (!out.includes(it.text)) out.push(it.text)
  }
  return out
})
// 探过了、一个开关都没有：思考/联网 就该锁在「未绑定」，不用从 chip 里硬挑
const noToggleUi = computed(() => probeItems.value.length > 0 && toggleCandidates.value.length === 0)
const GUESS_WORDS = { thinking: ['思考', '推理', '深度'], search: ['联网', '搜索', '网络'] }
function guessOf(feature) {
  const words = GUESS_WORDS[feature] || []
  return toggleCandidates.value.filter((t) => words.some((w) => t.includes(w)))
}
const modelLabelSet = computed(() => new Set(modelLabels.value.map((m) => (m.label || '').trim()).filter(Boolean)))
// 与后端 ui_modes._misbind_hint 同口径：撞模型文案 / 两个语义撞同一个控件
const bindAudit = computed(() => {
  const bad = []
  const tips = []
  const pairs = [
    ['思考', bindThinking.value, 'thinking'],
    ['联网', bindSearch.value, 'search'],
  ]
  for (const [name, picked, feature] of pairs) {
    if (!picked) continue
    if (modelLabelSet.value.has(picked)) {
      bad.push(`「${name}」绑的「${picked}」是模型下拉的文案，不是开关 —— 改回「未绑定」`)
    } else if (!noToggleUi.value && !guessOf(feature).includes(picked)) {
      tips.push(`「${name}」绑的「${picked}」文案里没有开关字样，确认它就是那个开关`)
    }
  }
  if (bindThinking.value && bindThinking.value === bindSearch.value) {
    bad.push(`思考与联网绑了同一个控件「${bindThinking.value}」，至少有一个不对`)
  }
  const trigger = bindModelTrigger.value.trim()
  if (trigger && [bindThinking.value, bindSearch.value].includes(trigger)) {
    bad.push(`模型触发器与开关绑了同一个控件「${trigger}」`)
  }
  if (!noToggleUi.value) {
    for (const [name, picked, feature] of pairs) {
      const g = guessOf(feature)
      if (!picked && g.length) tips.push(`「${name}」没绑，探测里有 ${g.slice(0, 3).join('、')}${g.length > 3 ? '…' : ''} 看着像`)
    }
  } else {
    tips.push('这个页面没探到任何带开/关状态的控件：思考/联网 保持未绑定就行，不用硬挑一个')
  }
  return { bad, tips, all: [...bad, ...tips] }
})
const bindNotes = computed(() => bindAudit.value.all)
const bindBad = computed(() => bindAudit.value.bad.length > 0)
// 高级里的绑定收成一行摘要摆在主行上，折叠了也看得见配过什么
const advSummary = computed(() => {
  const parts = []
  if (bindThinking.value) parts.push('思考=' + bindThinking.value)
  if (bindSearch.value) parts.push('联网=' + bindSearch.value)
  for (const c of customFeatures.value) parts.push(c.key + '=' + c.text)
  return parts.length ? parts.join('  ') : '思考/联网/场景：均未绑定'
})

// ---- 点 chip 直接绑定 ----
const chipMenu = ref(null)
const chipTargets = computed(() => {
  const out = [
    { key: 'thinking', label: '思考开关', hint: bindThinking.value ? '（换掉现有绑定）' : '' },
    { key: 'search', label: '联网开关', hint: bindSearch.value ? '（换掉现有绑定）' : '' },
    { key: 'model', label: '模型触发器', hint: bindModelTrigger.value ? '（换掉现有绑定）' : '' },
    { key: 'custom', label: '新增为场景', hint: '代码/翻译这类开关' },
  ]
  for (const m of modelLabels.value.slice(0, 6)) {
    out.push({ key: 'model:' + m.id, label: '模型 ' + m.id, hint: m.label ? '现：' + m.label : '' })
  }
  return out
})
function chipUsed(text) {
  const bound = [
    bindThinking.value,
    bindSearch.value,
    bindModelTrigger.value.trim(),
    ...modelLabels.value.map((m) => (m.label || '').trim()),
    ...customFeatures.value.map((c) => c.text),
  ].filter(Boolean)
  return bound.includes(text)
}
function chipTitle(it) {
  const base = probeTitle(it)
  return (chipUsed(it.text) ? '已绑定 · 点击可改绑' : '点击把它绑给…') + (base ? ' · ' + base : '')
}
function openChipMenu(it, ev) {
  if (!it.text) return
  const x = Math.max(8, Math.min(ev.clientX, window.innerWidth - 240))
  const y = Math.max(8, Math.min(ev.clientY + 10, window.innerHeight - 300))
  chipMenu.value = { text: it.text, x, y }
}
function applyChip(key) {
  const text = chipMenu.value ? chipMenu.value.text : ''
  chipMenu.value = null
  if (!text) return
  if (key === 'thinking' || key === 'search') {
    if (noToggleUi.value) {
      appendLog(`提醒：这个页面没探到带开/关状态的控件，「${text}」多半不是开关`, 'err')
    }
    if (key === 'thinking') bindThinking.value = text
    else bindSearch.value = text
    showAdvanced.value = true
  } else if (key === 'model') {
    bindModelTrigger.value = text
  } else if (key === 'custom') {
    showAdvanced.value = true
    customPick.value = text
    appendLog(`场景候选已填入「${text}」：补个英文键名（如 code）再点「添加」`)
    return
  } else if (key.startsWith('model:')) {
    const m = modelLabels.value.find((x) => x.id === key.slice(6))
    if (!m) return
    m.label = text
  } else {
    return
  }
  const t = chipTargets.value.find((o) => o.key === key)
  appendLog(`已把「${text}」填进「${t ? t.label : key}」，点「保存绑定」才生效`)
}

const modeOptions = computed(() => {
  const list = bindingModes.value.length ? bindingModes.value : STD_MODES
  return list.map((m) => ({ id: m.id, label: m.label || m.id }))
})
let ws = null
// 这条 socket 是给哪个站点开的 + 什么时候开始握手：connect() 靠它们判断「同站且还活着」就不再开第二条
let wsProvider = ''
let wsStartedAt = 0
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
  // 同一个站点已经连着/正在握手：直接返回。
  // 控制台点 Provider 卡片时，onMounted（autoConnect）与父组件的 nextTick(openProvider)
  // 会各叫一次；握手还没完成时只看 connected 拦不住，会关掉第一条再造一条，
  // 旧 socket 的 onclose 晚到还会把 connected 重置成 false。
  // 10 秒还没握上就不再护着它（后端卡住时人要能手动重连）。
  const alive = ws && wsProvider === provider.value
    && (ws.readyState === WebSocket.OPEN
      || (ws.readyState === WebSocket.CONNECTING && Date.now() - wsStartedAt < 10000))
  if (alive) return
  if (ws) { ws.close(); ws = null }
  // 换一个站点就是一个新页面，上次的探测结果留着会误导
  probeItems.value = []
  bindingLoaded.value = false
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
  const sock = new WebSocket(`${proto}://${location.host}/ws/live/${provider.value}${qs}`)
  ws = sock
  wsProvider = provider.value
  wsStartedAt = Date.now()
  // 所有回调先校验「还是不是当前 socket」：重连时旧连接的 close/error 晚到，
  // 不拦就会把新连接的 connected/statusText 改回去，表现为「画面在动但显示未连接」。
  sock.onopen = () => {
    if (ws !== sock) return
    connected.value = true
    statusText.value = '已连接'
    appendLog('连接成功')
    loadBindings()
  }
  sock.onclose = () => {
    if (ws !== sock) return
    connected.value = false
    statusText.value = '未连接'
    frameSrc.value = ''
    appendLog('连接已断开')
  }
  sock.onerror = () => {
    if (ws !== sock) return
    statusText.value = '连接错误'
    appendLog('WebSocket 连接错误')
  }
  sock.onmessage = (ev) => {
    if (ws !== sock) return
    const d = JSON.parse(ev.data)
    if (d.t === 'meta') { logicalW = d.w || 1280; logicalH = d.h || 800; dsf = d.dsf || 1 }
    else if (d.t === 'frame') { frameSrc.value = 'data:image/jpeg;base64,' + d.data }
    else if (d.t === 'log') { statusText.value = d.message; appendLog(d.message) }
    else if (d.t === 'error') { appendLog('错误：' + d.message, 'err') }
    else if (d.t === 'autoslide_result') {
      const r = d.result || {}
      // 让位不算失败：是后端检测到人在画面里动手，主动停下了自动拖动
      if (r.handoff) appendLog('已让位给手动操作：' + (r.message || ''))
      else appendLog((r.ok ? '✓ ' : '✗ ') + (r.message || '未知结果'))
    }
    else if (d.t === 'record_result') {
      const r = d.result
      if (!r) appendLog('录制结束：未录制到数据')
      // 后端的 message 会区分「只抓到风控/滑块流量」和「压根没发请求」，
      // 这两者的处理办法完全不同，不能笼统成一句「未抓到对话请求」
      else if (r.skipped) appendLog('录制结束：' + (r.message || '未抓到对话请求，已保留原 fixtures'), 'err')
      else {
        appendLog(`录制完成：识别到 ${r.count} 个请求，端点=${r.endpoint || '未识别'}，已写入 ${r.file}`)
        if (r.risk_skipped) appendLog(`  · 已顺带排除 ${r.risk_skipped} 条风控/滑块流量（不会被当成对话接口）`)
      }
      recording.value = false
    }
  }
}

function closeSocket() {
  if (ws) { ws.close(); ws = null }
  wsProvider = ''
  wsStartedAt = 0
  connected.value = false
  statusText.value = '未连接'
  frameSrc.value = ''
  recording.value = false
  probing.value = false
  applying.value = false
  probeItems.value = []
}

function disconnect() {
  // 用户主动断开：下次进来不要再自动连
  try { localStorage.setItem(WANT_KEY, '0') } catch (_) { /* 忽略 */ }
  closeSocket()
  appendLog('已断开画面：浏览器仍在后台运行（控制台「状态」里的浏览器仍亮），下次连接会秒开；要彻底关掉请点「关闭浏览器」')
}

async function closeBrowser() {
  // 断开只停推屏，后台浏览器/标签页还在（后台聊天要复用，也能避免反复重启）。
  // 要彻底关掉走 full=1：控制台「状态」里的浏览器才会变灰。
  try { localStorage.setItem(WANT_KEY, '0') } catch (_) { /* 忽略 */ }
  closeSocket()
  const headers = apiHeaders()
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

// ---- 页面模式：探测文案 / 一键对齐 ----
function apiHeaders(json) {
  // 口令统一从 utils/api 取，服务端没设 W2A_API_KEY 时不额外带头
  return authHeaders(json ? { 'Content-Type': 'application/json' } : {})
}

async function readError(r) {
  // 409/403 正文里写着「live 未打开」这类原因，只报状态码用户没法排查
  let msg = 'HTTP ' + r.status
  try {
    const j = await r.json()
    msg = (j && j.error && j.error.message) || j?.detail || msg
    if (j && j.hint) msg += '（' + j.hint + '）'
  } catch (_) { /* 非 JSON 响应就只用状态码 */ }
  return msg
}

function probeOn(it) {
  return it.pressed === 'true' || it.checked === 'true'
}

function probeState(it) {
  const v = it.pressed != null ? it.pressed : it.checked
  if (v == null) return ''
  return v === 'true' ? '开' : '关'
}

function probeTitle(it) {
  const parts = []
  if (it.role) parts.push('role=' + it.role)
  if (it.pressed != null) parts.push('aria-pressed=' + it.pressed)
  if (it.checked != null) parts.push('aria-checked=' + it.checked)
  if (it.cls) parts.push(it.cls)
  return parts.join(' · ')
}

async function probeModes() {
  if (!connected.value) return
  probing.value = true
  try {
    const r = await fetch(`/live/ui-probe/${provider.value}`, { headers: apiHeaders() })
    if (!r.ok) throw new Error(await readError(r))
    const j = await r.json()
    probeItems.value = j.items || []
    // 探不到任何带开/关状态的控件：上一轮硬挑的开关绑定就是错的，直接改回未绑定
    // （不靠禁用下拉拦人：禁了就没法把旧值清掉了）
    if (noToggleUi.value && (bindThinking.value || bindSearch.value)) {
      appendLog(`这个页面没有带开/关状态的控件，已把思考/联网改回未绑定（还需点「保存绑定」生效）`)
      bindThinking.value = ''
      bindSearch.value = ''
      showAdvanced.value = true
    }
    appendLog(`探测到 ${probeItems.value.length} 个可点控件；直接点某个控件就能把它绑给思考/联网/模型，带开/关状态的才能当开关（悬停可看 aria/class）`)
  } catch (e) {
    appendLog('探测失败：' + e.message, 'err')
  } finally {
    probing.value = false
  }
}

async function applyPreset() {
  if (!connected.value) return
  applying.value = true
  try {
    const r = await fetch(`/live/mode/${provider.value}`, {
      method: 'POST',
      headers: apiHeaders(true),
      body: JSON.stringify({ mode: modePreset.value }),
    })
    if (!r.ok) throw new Error(await readError(r))
    const j = await r.json()
    const label = j.mode || modePreset.value
    if (j.ok) appendLog(`✓ 模式已对齐 ${label}，用时 ${j.ms}ms`)
    else appendLog(`✗ 模式部分未对齐 ${label}，缺失=${(j.missing || []).join('/')}（先点「重载配置」让新文案生效；仍失败就重新探测校准）`, 'err')
    // 绑错单独报：后端已经把它跳过了（不去碰模型下拉），但配置本身仍需要修
    if (j.misbind && j.misbind.length) {
      appendLog(`✗ 疑似绑错：${j.misbind.join('/')} 绑的文案不是这个开关，目标为关时已直接跳过（点「高级」改回未绑定）`, 'err')
    }
    for (const d of j.details || []) {
      appendLog(`  · ${d.feature} want=${d.want} ${d.action || d.reason || ''}`)
    }
  } catch (e) {
    appendLog('应用模式失败：' + e.message, 'err')
  } finally {
    applying.value = false
  }
}

async function reloadConfig() {
  reloading.value = true
  try {
    const r = await fetch('/admin/reload', { method: 'POST', headers: apiHeaders() })
    if (!r.ok) throw new Error(await readError(r))
    const j = await r.json()
    const models = Object.entries(j.models || {}).map(([k, v]) => `${k}×${v}`).join(' ')
    appendLog(`✓ 配置已重载：${(j.providers || []).length} 个 provider，模型 ${models || '-'}`)
    // 旧探测结果对应旧配置，先清掉；当前 provider 有变化时顺手重探一次，省一步手点
    probeItems.value = []
    const touched = [...(j.added || []), ...(j.updated || [])]
    if (connected.value && touched.includes(provider.value)) await probeModes()
    else if (connected.value) appendLog('当前 provider 未被更新（可能已从配置移除），跳过重新探测')
  } catch (e) {
    const tip = /HTTP 401/.test(e.message) ? '（先在左下角钥匙里填访问口令）' : ''
    appendLog('重载配置失败：' + e.message + tip, 'err')
  } finally {
    reloading.value = false
  }
}

// ---- 页面内绑定：探测到的文案 -> 思考/联网/模型/自定义场景 -> storage/ui-bindings.yaml ----
function applyBindings(j) {
  bindingLoaded.value = true
  bindingsFile.value = j.bindings_file || ''
  const ui = j.ui || {}
  const pools = {}
  const extras = {}
  const custom = []
  for (const [name, ctl] of Object.entries(ui)) {
    pools[name] = [...(ctl.labels || [])]
    const extra = {}
    if (ctl.active_class) extra.active_class = ctl.active_class
    if (ctl.blind) extra.blind = true
    extras[name] = extra
    if (!STD_FEATURES.includes(name)) custom.push({ key: name, text: pools[name][0] || name })
  }
  uiPools.value = pools
  uiExtras.value = extras
  customFeatures.value = custom
  bindThinking.value = pools.thinking?.[0] || ''
  bindSearch.value = pools.search?.[0] || ''
  bindModelTrigger.value = pools.model?.[0] || ''
  modelLabels.value = (j.models || []).map((m) => ({ id: m.id, label: m.label || '' }))
  bindingModes.value = j.modes || []
  if (!bindingModes.value.some((m) => m.id === modePreset.value)) modePreset.value = 'quick'
  // 已经配过开关/场景就不能藏着，否则用户看不到自己绑了什么；全空时保持折叠
  if (bindThinking.value || bindSearch.value || custom.length) showAdvanced.value = true
}

function bindPayload(name, picked) {
  if (!picked) return null
  const labels = []
  for (const v of [picked, ...(uiPools.value[name] || [])]) {
    if (v && !labels.includes(v)) labels.push(v)
  }
  return { labels, ...uiExtras.value[name] }
}

function addCustom() {
  const text = customPick.value
  const key = customKey.value.trim().toLowerCase()
  if (!text || !key) return
  if (!/^[a-z][a-z0-9_]{0,23}$/.test(key)) {
    appendLog('键名只能用 a-z、0-9、_，以小写字母开头，最长 24 位', 'err')
    return
  }
  if (RESERVED_KEYS.includes(key)) {
    appendLog(`「${key}」是内置开关/模式名，换一个，比如 code / translate`, 'err')
    return
  }
  if (customFeatures.value.some((c) => c.key === key)) {
    appendLog(`键名 ${key} 已经绑过了`, 'err')
    return
  }
  if (customFeatures.value.length >= 8) {
    appendLog('最多绑定 8 个自定义场景', 'err')
    return
  }
  customFeatures.value.push({ key, text })
  customPick.value = ''
  customKey.value = ''
  appendLog(`已添加场景「${text}」→ ${key}，点「保存绑定」生效`)
}

function removeCustom(key) {
  customFeatures.value = customFeatures.value.filter((c) => c.key !== key)
  appendLog(`已移除场景 ${key}（点「保存绑定」生效）`)
}

async function loadBindings() {
  try {
    const r = await fetch(`/live/ui-bindings/${provider.value}`, { headers: apiHeaders() })
    if (!r.ok) return
    applyBindings(await r.json())
  } catch (_) { /* 绑定面板失败不影响聊天 */ }
}

async function saveBindings() {
  // 后端会把“绑了模型文案且目标为关”的开关直接跳过不点，所以误绑不会每轮去碰模型下拉；
  // 这里只提醒不拦阻，用户确实可能有理由复用同一个文案
  for (const w of bindAudit.value.bad) appendLog('提醒：' + w, 'err')
  savingBindings.value = true
  try {
    const ui = {}
    const th = bindPayload('thinking', bindThinking.value)
    if (th) ui.thinking = th
    const se = bindPayload('search', bindSearch.value)
    if (se) ui.search = se
    const trigger = bindModelTrigger.value.trim()
    const mo = bindPayload('model', trigger)
    if (mo) ui.model = mo
    for (const c of customFeatures.value) {
      const item = bindPayload(c.key, c.text)
      if (item) ui[c.key] = item
    }
    const models = {}
    for (const m of modelLabels.value) if (m.label.trim()) models[m.id] = m.label.trim()
    const r = await fetch(`/live/ui-bindings/${provider.value}`, {
      method: 'POST',
      headers: apiHeaders(true),
      body: JSON.stringify({ ui, models }),
    })
    if (!r.ok) throw new Error(await readError(r))
    applyBindings(await r.json())
    const scenes = customFeatures.value.map((c) => `${c.text}=${c.key}`).join(' ')
    appendLog(`✓ 绑定已保存并生效：思考=${bindThinking.value || '无'}，联网=${bindSearch.value || '无'}，模型=${trigger || '不切'}${scenes ? `，场景 ${scenes}` : ''}`)
    appendLog('  点「应用模式」验证；要回退到 config.yaml 的写法就点「清除」')
    if (connected.value) await probeModes()
  } catch (e) {
    appendLog('保存绑定失败：' + e.message, 'err')
  } finally {
    savingBindings.value = false
  }
}

async function clearBindings() {
  savingBindings.value = true
  try {
    const r = await fetch(`/live/ui-bindings/${provider.value}`, { method: 'DELETE', headers: apiHeaders() })
    if (!r.ok) throw new Error(await readError(r))
    applyBindings(await r.json())
    appendLog('✓ 已清除页面绑定，回到 config.yaml 的写法')
    if (connected.value) await probeModes()
  } catch (e) {
    appendLog('清除绑定失败：' + e.message, 'err')
  } finally {
    savingBindings.value = false
  }
}

function autoSlide() {
  if (!connected.value || !ws) return
  appendLog('开始自动拖动滑块…')
  ws.send(JSON.stringify({ t: 'autoslide' }))
}

async function clearCooldown() {
  try {
    const r = await fetch(`/live/cooldown/clear/${provider.value}`, { method: 'POST', headers: apiHeaders() })
    appendLog(r.ok ? '已清除风控冷却，可以立即重试聊天' : '清除冷却失败：HTTP ' + r.status)
  } catch (e) {
    appendLog('清除冷却失败：' + e)
  }
}

/**
 * 画面像素 -> 远端视口坐标。
 * drag=true（拖拽中）时不夹在画面边界：手拖出去还在往前推，这正是站点需要的
 * 「仍在移动」信号；夹回去会变成一堆重复坐标，轨迹看着像停顿，对风控是坏信号。
 */
function toLogical(e, drag = false) {
  const el = stageEl.value?.querySelector('img')
  if (!el) return { x: 0, y: 0 }
  const r = el.getBoundingClientRect()
  let fx = (e.clientX - r.left) / r.width
  let fy = (e.clientY - r.top) / r.height
  if (drag) {
    fx = Math.max(-0.25, Math.min(1.25, fx))
    fy = Math.max(-0.25, Math.min(1.25, fy))
  } else {
    fx = Math.max(0, Math.min(1, fx))
    fy = Math.max(0, Math.min(1, fy))
  }
  return { x: fx * (logicalW / dsf), y: fy * (logicalH / dsf) }
}

function sendMouse(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg))
}

function onMouseDown(e) {
  e.preventDefault()
  imeEl.value?.focus() // 焦点给它，中文输入法才能正常工作
  dragging = true
  // 拖拽中把移动挂到 window：绑在 <img> 上时手一快拖出画面就彻底断流，
  // 远端再也收不到后续移动，手柄僵在半路直到抬手 —— 手动过滑块就是这么失败的。
  window.addEventListener('mousemove', onDragMove)
  lastPointer = { clientX: e.clientX, clientY: e.clientY }
  const p = toLogical(e, true)
  sendMouse({ t: 'mouse', action: 'down', x: p.x, y: p.y, button: 'left', clickCount: 1 })
}

let dragging = false
function onMouseUp(e) {
  if (!dragging) return
  endDrag(e)
}

function endDrag(e) {
  dragging = false
  window.removeEventListener('mousemove', onDragMove)
  lastPointer = { clientX: e.clientX, clientY: e.clientY }
  flushMove() // 先把攒着没发的最后一帧补出去，否则 up 的坐标比手慢一帧
  const p = toLogical(e, true)
  sendMouse({ t: 'mouse', action: 'up', x: p.x, y: p.y, button: 'left', clickCount: 1 })
}

// 在窗口外松手时页面收不到 mouseup，远端会一直以为左键按着（滑块卡在拖住状态，
// 之后的操作全都跟着坏）；失焦时兜底补一次抬手。
function onBlur() {
  if (dragging) endDrag(lastPointer)
}

// ---- 移动上报：按帧合批 ----
// 原来用 Date.now() 卡 16ms，既和屏幕刷新率不同步，又会把收尾那几个点直接丢掉。
let moveRaf = 0
let pendingMove = null
let lastMove = 0
// 最后一次已知的指针位置：失焦兜底抬手要用它，不能退到 (0,0) 去点一个无关的地方
let lastPointer = { clientX: 0, clientY: 0 }

function emitMove(x, y, drag) {
  const q = toLogical({ clientX: x, clientY: y }, drag)
  const msg = { t: 'mouse', action: 'move', x: q.x, y: q.y }
  if (drag) {
    // 关键：拖拽时必须标记左键按住，否则 CDP 不认为是拖动，滑块不跟手
    msg.button = 'left'
    msg.clickCount = 1
  }
  sendMouse(msg)
}

function queueMove(e, drag) {
  lastPointer = { clientX: e.clientX, clientY: e.clientY }
  pendingMove = { x: e.clientX, y: e.clientY, drag }
  if (moveRaf) return // 本帧已有待发的，只把坐标换成最新的
  moveRaf = requestAnimationFrame(() => {
    moveRaf = 0
    const p = pendingMove
    pendingMove = null
    if (p) emitMove(p.x, p.y, p.drag)
  })
}

function flushMove() {
  if (!moveRaf) return
  cancelAnimationFrame(moveRaf)
  moveRaf = 0
  const p = pendingMove
  pendingMove = null
  if (p) emitMove(p.x, p.y, p.drag)
}

function onDragMove(e) {
  if (dragging) queueMove(e, true)
}

// 悬停（没按住）保持原来的低频上报；拖拽中的移动由 window 上的 onDragMove 接手，
// 同一次移动会冒到这里，必须让开，否则一帧发两份、轨迹里多出没有的抖动
function onMouseMove(e) {
  if (dragging) return
  const now = Date.now()
  if (now - lastMove < 50) return
  lastMove = now
  queueMove(e, false)
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
  window.addEventListener('blur', onBlur)
  // 带 provider 进来（Provider 卡片 / 老 /live 链接）或上次会话还开着：直接恢复连接
  if (!props.active) return
  if (props.autoConnect || wantReconnect()) {
    appendLog('自动恢复上次连接：' + provider.value)
    connect()
  }
})

// 控制台切 tab 不销毁本组件（v-show），挂载钩子不会再来一次：
// 隐藏时停流省电（CDP 推屏是持续流量），切回来重连，provider 选择保留。
// 注意：这不算「用户主动断开」，不碰 WANT_KEY，否则切走一次就再也不自动连了。
let resumeOnShow = false
watch(() => props.active, (on) => {
  if (on) {
    if (!resumeOnShow) return
    resumeOnShow = false
    appendLog('回到「浏览器」：重新连接画面')
    connect()
    return
  }
  if (ws || connected.value) {
    resumeOnShow = true
    closeSocket()
    appendLog('已切走：暂停推屏（切回来自动重连）')
  }
})
onUnmounted(() => {
  window.removeEventListener('mouseup', onMouseUp)
  window.removeEventListener('mousemove', onDragMove)
  window.removeEventListener('blur', onBlur)
  if (moveRaf) cancelAnimationFrame(moveRaf)
  closeSocket()
})

// 推流状态外显：控制台的 tab 上有个点，切到别的 tab 也知道浏览器还开着
watch(connected, (v) => emit('update:connected', v))

// 父组件点 Provider 卡片时直接切到某个 provider 并连上（不只是改下拉值）
function openProvider(p) {
  if (p) provider.value = p
  connect()
}
defineExpose({ openProvider })
</script>

<style scoped>
.live-console {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg);
}
.live-main {
  flex: 1;
  min-width: 0;
  /* 日志区固定高度，剩下的都给画面：没有 min-height:0 时 flex 子项会撑破容器 */
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.live-note {
  margin: 0;
  padding: 7px 20px;
  border-bottom: 1px solid var(--line-soft);
  background: var(--panel);
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
  background: var(--scrim-raised);
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
  background: var(--panel);
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
  background: var(--panel-2);
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
  accent-color: var(--run);
  cursor: pointer;
}
.auto-send.on {
  color: var(--text-muted);
}

/* 页面模式 */
.mode-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 9px 20px;
  border-bottom: 1px solid var(--line-soft);
  background: var(--panel);
}
.mode-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.mode-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
}
.mode-hint {
  font-size: 11px;
  color: var(--text-faint);
}
.mode-spacer {
  flex: 1;
}
.mode-select {
  width: 176px;
  height: 28px;
}
.probe-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 92px;
  overflow-y: auto;
}
.probe-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid var(--line-soft);
  background: var(--panel-2);
  font-size: 11.5px;
  color: var(--text-muted);
  cursor: pointer;
}
.probe-chip:hover {
  border-color: var(--line-strong);
  color: var(--text);
}
/* 已经绑给某个语义的控件：标出来，避免同一个文案绑两处 */
.probe-chip.used {
  border-color: rgba(var(--sky-rgb), 0.4);
  background: rgba(var(--sky-rgb), 0.08);
  color: var(--info-fg);
}
.probe-chip.on {
  border-color: rgba(var(--run-rgb), 0.35);
  background: rgba(var(--run-rgb), 0.08);
  color: var(--run-fg);
}
.probe-chip b {
  font-weight: 500;
  font-size: 10.5px;
  color: var(--text-faint);
}
.probe-chip.on b {
  color: inherit;
}
.bind-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding-top: 2px;
}
.bind-title {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-faint);
}
.bind-field {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
}
.bind-select {
  width: 126px;
  height: 26px;
  padding-right: 24px;
  font-size: 11.5px;
}
.bind-input {
  width: 150px;
  height: 26px;
  font-size: 11.5px;
}
.bind-key {
  width: 118px;
}
.bind-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px 2px 8px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
}
.bind-tag b {
  color: var(--run-fg);
  font-weight: 600;
}
.bind-x {
  border: 0;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  padding: 0 3px;
}
.bind-x:hover {
  color: var(--danger);
}
/* 高级里配了什么，折起来也能一眼看到 */
.bind-sum {
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
}
.bind-adv {
  padding-left: 44px;
  padding-top: 0;
}
/* 绑定体检结果：有硬错时标红，否则只是淡灰提示 */
.bind-notes {
  margin: 4px 0 0;
  padding-left: 44px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-faint);
}
.bind-notes.is-bad {
  color: var(--danger-fg);
}
.chip-mask {
  position: fixed;
  inset: 0;
  z-index: 210;
}
.chip-pop {
  position: fixed;
  width: 224px;
  padding: 8px;
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  background: var(--panel);
  box-shadow: 0 18px 44px rgba(var(--shade-rgb), 0.55);
}
.chip-pop-t {
  margin: 2px 4px 8px;
  font-size: 11.5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chip-pop-i {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.chip-pop-i:hover {
  border-color: var(--line);
  background: var(--panel-2);
}
.chip-pop-i em {
  font-style: normal;
  font-size: 10.5px;
  color: var(--text-faint);
}
.chip-pop-x {
  margin-top: 4px;
  color: var(--text-faint);
  border-top: 1px solid var(--line-soft);
  border-radius: 0 0 8px 8px;
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
  background: var(--panel-3);
}
.stage-dots span:first-child { background: var(--line-strong); }
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
  background-color: var(--bg);
  background-image: radial-gradient(rgba(var(--run-rgb), 0.08) 1px, transparent 1px);
  background-size: 22px 22px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03), 0 30px 60px -40px rgba(var(--shade-rgb), 0.9);
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
  background: var(--panel);
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
  background: var(--panel);
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
  color: var(--text-faint);
  flex-shrink: 0;
}
.log-text {
  color: var(--text-muted);
  white-space: pre-wrap;
  word-break: break-word;
}
.log-line.ok .log-text { color: var(--teal); }
.log-line.err .log-text { color: var(--danger-fg); }
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
