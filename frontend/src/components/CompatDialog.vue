<template>
  <Teleport to="body">
    <div v-if="open" class="cd-mask" @mousedown.self="close">
      <section class="cd-box" role="dialog" aria-modal="true" aria-labelledby="cd-title">
        <header class="cd-head">
          <h2 id="cd-title">{{ isNew ? '新增直连通道' : '配置 ' + (form.display_name || form.key) }}</h2>
          <button class="cd-x" type="button" aria-label="关闭" @click="close">
            <AppIcon name="x" :size="16" />
          </button>
        </header>

        <p class="cd-hint">
          直连通道走 OpenAI 兼容接口，一条 HTTP 请求打上游，不拉浏览器。
          key 存在本地库（storage/，不进 git），界面只回掩码；填在这里就行，不用去动
          <code>config.yaml</code> 或 <code>.env</code>。
        </p>

        <label v-if="isNew" class="cd-field">
          <span>通道 key（小写字母/数字/连字符，如 deepseek-api）</span>
          <input v-model="form.key" type="text" autocomplete="off" spellcheck="false" placeholder="deepseek-api" />
        </label>

        <label class="cd-field">
          <span>显示名</span>
          <input v-model="form.display_name" type="text" autocomplete="off" placeholder="DeepSeek 官方" />
        </label>

        <label class="cd-field">
          <span>base_url（兼容端点根地址，到 /v1 为止）</span>
          <input v-model="form.base_url" type="text" autocomplete="off" spellcheck="false" placeholder="https://api.deepseek.com/v1" />
        </label>

        <label class="cd-field">
          <span>API key</span>
          <input
            v-model="form.api_key"
            type="password"
            autocomplete="new-password"
            spellcheck="false"
            :placeholder="keyPlaceholder"
          />
          <span v-if="!isNew && entry?.key_source === 'db'" class="cd-sub">当前：{{ entry.key_masked }}</span>
        </label>

        <label class="cd-field">
          <span>模型 id（一行一个，对话与批量生成的下拉就会出现它们）</span>
          <textarea v-model="form.modelsText" rows="4" spellcheck="false" placeholder="deepseek-chat&#10;deepseek-reasoner"></textarea>
        </label>

        <label class="cd-check">
          <input v-model="form.enabled" type="checkbox" />
          启用（停用 = 不注册路由，配置留着）
        </label>

        <p v-if="msg" class="cd-msg" :class="{ 'is-bad': msgBad }">{{ msg }}</p>

        <footer class="cd-foot">
          <button
            v-if="!isNew"
            class="cd-btn"
            type="button"
            :disabled="busy || testing"
            @click="test"
          >
            {{ testing ? '测试中…' : '测试连通' }}
          </button>
          <span class="cd-spacer"></span>
          <button class="cd-btn" type="button" :disabled="busy" @click="close">取消</button>
          <button class="cd-btn primary" type="button" :disabled="busy" @click="save">
            {{ busy ? '保存中…' : '保存并生效' }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { apiFetch } from '../utils/api'

const props = defineProps({
  open: Boolean,
  // null = 新增；对象 = /admin/compat 返回的一行（编辑）
  entry: { type: Object, default: null },
})
const emit = defineEmits(['update:open', 'saved'])

const form = ref({ key: '', display_name: '', base_url: '', api_key: '', modelsText: '', enabled: true })
const msg = ref('')
const msgBad = ref(false)
const busy = ref(false)
const testing = ref(false)

const isNew = computed(() => !props.entry)
// 编辑时留空 = 保持库里原 key（PUT 的语义：不传就不动）；清除走「传空串」，
// 界面上不给顺手误触的入口——要给就单做一个明确按钮，这版先不做
const keyPlaceholder = computed(() => (isNew.value ? 'sk-...；本地 Ollama 可留空' : '留空 = 保持不变'))

watch(
  () => props.open,
  (v) => {
    if (!v) return
    const e = props.entry
    form.value = {
      key: e?.provider || '',
      display_name: e?.display_name || '',
      base_url: e?.base_url || '',
      api_key: '',
      modelsText: (e?.models || []).join('\n'),
      enabled: e ? Boolean(e.enabled) : true,
    }
    msg.value = ''
    msgBad.value = false
    testing.value = false
  },
)

function close() {
  if (!busy.value) emit('update:open', false)
}

function parseModels() {
  return form.value.modelsText.split('\n').map(s => s.trim()).filter(Boolean)
}

// 后端错误统一是一句话 detail；HTTPException 的 400/404 与业务 200 里的 ok=false 都走这。
// 422 的 detail 是数组（FastAPI 校验报告），摊平成可读的一句话，别让人只看见 HTTP 422
async function readError(r) {
  const j = await r.json().catch(() => null)
  const d = j && j.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d)) {
    return d.map(e => `${(e.loc || []).slice(1).join('.') || 'body'}: ${e.msg}`).join('；')
  }
  return 'HTTP ' + r.status
}

async function save() {
  const models = parseModels()
  const body = {
    display_name: form.value.display_name.trim(),
    base_url: form.value.base_url.trim(),
    models,
    enabled: form.value.enabled,
  }
  // api_key 只在真输入了才传：空着提交等于「没动这个字段」
  if (form.value.api_key.trim()) body.api_key = form.value.api_key.trim()

  busy.value = true
  msg.value = ''
  msgBad.value = false
  try {
    let r
    // Content-Type 必须显式带：fetch 对字符串 body 不会自动加，FastAPI 认不出
    // JSON 就当 body 缺失，保存好使不好使全在这一个头上（实测栽过 422）
    const jsonOpts = { headers: { 'Content-Type': 'application/json' } }
    if (isNew.value) {
      body.key = form.value.key.trim().toLowerCase()
      if (!body.key) throw new Error('先给通道起个 key（如 deepseek-api）')
      r = await apiFetch('/admin/compat', { method: 'POST', ...jsonOpts, body: JSON.stringify(body) })
    } else {
      r = await apiFetch('/admin/compat/' + encodeURIComponent(props.entry.provider), {
        method: 'PUT', ...jsonOpts, body: JSON.stringify(body),
      })
    }
    if (!r.ok) throw new Error(await readError(r))
    emit('saved', await r.json().catch(() => null))
  } catch (e) {
    msgBad.value = true
    msg.value = '保存失败：' + e.message
  } finally {
    busy.value = false
  }
}

async function test() {
  testing.value = true
  msg.value = ''
  msgBad.value = false
  try {
    const body = form.value.api_key.trim() ? { api_key: form.value.api_key.trim() } : {}
    const r = await apiFetch('/admin/compat/' + encodeURIComponent(props.entry.provider) + '/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const j = await r.json().catch(() => null)
    if (!r.ok) throw new Error(await readError(r))
    msgBad.value = !j?.ok
    msg.value = j?.ok ? `上游通了 · ${j.latency_ms} ms` : `没通：${j?.detail || '未知'}`
  } catch (e) {
    msgBad.value = true
    msg.value = '测试失败：' + e.message
  } finally {
    testing.value = false
  }
}
</script>

<style scoped>
.cd-mask {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--overlay);
  backdrop-filter: blur(3px);
}
.cd-box {
  width: min(520px, 100%);
  max-height: 86vh;
  overflow: auto;
  padding: 18px 20px 16px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--panel);
  box-shadow: 0 22px 60px rgba(var(--shade-rgb), 0.55);
}
.cd-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.cd-head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.cd-x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.cd-x:hover {
  color: var(--text);
  border-color: var(--line);
}
.cd-hint {
  margin: 10px 0 14px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-faint);
}
.cd-hint code {
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--panel-2);
  font-family: theme('fontFamily.mono');
  font-size: 11px;
  color: var(--text-muted);
}
.cd-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--text-muted);
}
.cd-field input,
.cd-field textarea {
  padding: 8px 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius-control);
  background: var(--panel);
  color: var(--text);
  font-family: theme('fontFamily.mono');
  font-size: 12.5px;
  outline: none;
  resize: vertical;
}
.cd-field input:focus,
.cd-field textarea:focus {
  border-color: var(--run-dim);
  box-shadow: var(--ring);
}
.cd-sub {
  font-size: 11px;
  color: var(--text-faint);
}
.cd-check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted);
  cursor: pointer;
}
.cd-msg {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--run);
}
.cd-msg.is-bad {
  color: var(--danger-fg);
}
.cd-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
}
.cd-spacer {
  flex: 1;
}
.cd-btn {
  height: 32px;
  padding: 0 14px;
  border-radius: var(--radius-control);
  font-size: 12.5px;
  cursor: pointer;
  border: 1px solid var(--line);
  background: var(--panel-2);
  color: var(--text-muted);
}
.cd-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--line-strong);
}
.cd-btn.primary {
  border-color: rgba(var(--run-rgb), 0.4);
  background: rgba(var(--run-rgb), 0.14);
  color: var(--run-fg);
}
.cd-btn.primary:hover:not(:disabled) {
  background: rgba(var(--run-rgb), 0.22);
}
.cd-btn:disabled {
  opacity: 0.55;
  cursor: default;
}
</style>
