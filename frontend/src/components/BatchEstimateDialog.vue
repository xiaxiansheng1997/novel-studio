<template>
  <div v-if="open" class="mask" @click.self="close">
    <div class="dlg" role="dialog" aria-modal="true" aria-label="批量生成预估">
      <header class="dlg-head">
        <div class="head-main">
          <h2>{{ mode === 'batch' ? '批量入队前先看一眼' : '逐节生成前先看一眼' }}</h2>
          <p class="dlg-sub">{{ workTitle }}</p>
        </div>
        <button class="x" aria-label="关闭" @click="close">
          <AppIcon name="x" :size="15" />
        </button>
      </header>

      <div class="dlg-body">
        <p v-if="loading" class="muted">正在按本书的历史生成量估算…</p>

        <template v-else-if="est">
          <div class="nums">
            <div class="num">
              <span class="num-v">{{ est.sections }}</span>
              <span class="num-k">待生成的节</span>
            </div>
            <div class="num">
              <span class="num-v">{{ fmt(est.output_chars_total) }}</span>
              <span class="num-k">预计产出字</span>
            </div>
            <div class="num">
              <span class="num-v">{{ fmt(est.prompt_chars_total) }}</span>
              <span class="num-k">预计投喂字</span>
            </div>
            <div class="num">
              <span class="num-v" :class="{ 'is-over': est.over_budget }">
                <template v-if="est.priced">≈ {{ est.est_cost }}</template>
                <template v-else>不计钱</template>
              </span>
              <span class="num-k">{{ est.priced ? '预估花费（按单价表）' : '反代通道' }}</span>
            </div>
          </div>

          <p class="hint">
            {{ est.basis_note }}；模型 {{ est.model }}。
            字数→token 按中文约 2 : 1 粗算，这是预估不是账单。
          </p>
          <p v-if="!est.priced" class="hint">
            反代不产生账单，但它的成本是账号风控——批量挂反代请把节数控制在个位数。
          </p>

          <div class="budget">
            <label class="field">
              <span class="field-label">预算上限（0 = 不设限）</span>
              <input v-model.number="budgetDraft" class="input" type="number" min="0" step="0.5" />
            </label>
            <button class="btn btn-sm" :disabled="budgetSaving" @click="saveBudget">
              {{ budgetSaving ? '保存中…' : '存上限' }}
            </button>
          </div>
          <p v-if="budgetHint" class="err">{{ budgetHint }}</p>

          <p v-if="est.over_budget" class="err">
            预估 {{ est.est_cost }} 已超预算上限 {{ est.budget_limit }}：
            服务端会直接拒跑。收窄范围（少几节）或调高上限，
            确实要跑就勾下面这条。
            <label class="check">
              <input v-model="ignoreBudget" type="checkbox" />
              <span>我已看过预估，忽略预算上限强行开跑</span>
            </label>
          </p>
        </template>

        <p v-else class="err">预估接口没回话：照常可以开跑，但这一眼成本就看不成了。</p>
      </div>

      <footer class="dlg-foot">
        <button class="btn" @click="close">取消</button>
        <button class="btn btn-primary" :disabled="loading" @click="confirm">
          {{ mode === 'batch' ? '入队开跑' : '开始逐节生成' }}
        </button>
      </footer>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { useStudioStore } from '../stores/studio'

const props = defineProps({
  open: { type: Boolean, default: false },
  // pipeline = 当前页面 SSE 逐节生成；batch = 落库队列（关了页面也接着跑）
  mode: { type: String, default: 'pipeline' },
  // 预估参数（only_empty/limit/model）：由调用方传「开跑时用的同一份」，
  // 不能靠两边各自碰巧一样的默认值——改了默认就静默口径不一（审查 M1）
  params: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:open', 'confirm'])
const store = useStudioStore()

const est = ref(null)
const loading = ref(false)
const ignoreBudget = ref(false)
const budgetDraft = ref(0)
const budgetSaving = ref(false)

const workTitle = computed(() => store.work?.title || '未命名作品')

watch(() => props.open, async (open) => {
  if (!open) return
  est.value = null
  ignoreBudget.value = false
  loading.value = true
  try {
    est.value = await store.estimatePipeline({ ...props.params })
    budgetDraft.value = est.value?.budget_limit ?? (await store.getBudget()) ?? 0
  } finally {
    loading.value = false
  }
})

function fmt(n) {
  return (n || 0).toLocaleString()
}

async function saveBudget() {
  if (budgetSaving.value) return
  // 清空/输成非数字会得到 NaN，而后端 0 = 不设限：不设防的话
  // 点「存上限」反而是静默解除闸门（审查 m4）。要解除请显式输 0。
  // 注意 type=number 的输入框清空给的是 ''，Number('') 是 0——空串得单独拦。
  const raw = String(budgetDraft.value ?? '').trim()
  const v = Number(raw)
  if (!raw || !Number.isFinite(v) || v < 0) {
    showBudgetHint('要给个数字：0 = 不设限，清空不是取消上限')
    return
  }
  budgetSaving.value = true
  try {
    const ok = await store.setBudget(v)
    if (ok && est.value) {
      // 上限变了，超限判断跟着重算一次：不重算会让人按旧结论点确认
      est.value = await store.estimatePipeline({ ...props.params })
      if (est.value && !est.value.over_budget) ignoreBudget.value = false
    }
  } finally {
    budgetSaving.value = false
  }
}

const budgetHint = ref('')
let hintTimer = 0
function showBudgetHint(msg) {
  budgetHint.value = msg
  if (hintTimer) clearTimeout(hintTimer)
  hintTimer = setTimeout(() => { budgetHint.value = '' }, 4000)
}

function close() {
  emit('update:open', false)
}

function confirm() {
  emit('confirm', { ignoreBudget: ignoreBudget.value && est.value?.over_budget })
  close()
}
</script>

<style scoped>
/* 弹层骨架与 ExportDialog 同一套变量，保持一家人的样子 */
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
  width: min(520px, 100%);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--line);
  border-radius: var(--radius-stage, 16px);
  background: var(--panel);
  box-shadow: 0 32px 80px -32px rgba(var(--shade-rgb), 0.9);
}
.dlg-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--line-soft);
}
.head-main {
  flex: 1;
  min-width: 0;
}
.dlg-head h2 {
  margin: 0 0 4px;
  font-family: theme('fontFamily.serif');
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
}
.dlg-sub {
  margin: 0;
  font-family: theme('fontFamily.mono');
  font-size: 11.5px;
  color: var(--text-faint);
}
.x {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}
.x:hover {
  background: rgba(var(--over), 0.07);
  color: var(--text);
}
.dlg-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 16px 18px;
}
.dlg-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 18px 14px;
  border-top: 1px solid var(--line-soft);
}
.nums {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
  margin-bottom: 12px;
}
.num {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 8px;
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  text-align: center;
}
.num-v {
  font-family: theme('fontFamily.mono');
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.num-v.is-over {
  color: var(--danger, #d33);
}
.num-k {
  font-size: 10.5px;
  color: var(--text-faint);
}
.hint {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-faint);
}
.muted {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-faint);
}
.budget {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field-label {
  font-size: 11px;
  color: var(--text-faint);
}
.input {
  width: 140px;
}
.err {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--danger, #d33);
}
.err .check {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  color: var(--text);
}
.btn {
  padding: 6px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: transparent;
  color: var(--text);
  font-size: 12.5px;
  cursor: pointer;
}
.btn:disabled {
  opacity: 0.55;
  cursor: default;
}
.btn-sm {
  padding: 5px 10px;
  font-size: 12px;
}
.btn-primary {
  border-color: transparent;
  background: var(--accent, #336);
  color: var(--accent-fg, #fff);
}
</style>
