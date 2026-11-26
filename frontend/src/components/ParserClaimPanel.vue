<template>
  <section class="claim-panel" data-testid="parser-claim-panel" id="parser-claim-entry">
    <div class="claim-head">
      <h3>页面解析模板 · 领取回归样本试跑</h3>
      <p class="muted">
        解析模板从同一批导入回执中<b>领取合格回归样本</b>，按选择器/字段映射<b>真实提取价格字段</b>，
        生成带下载凭据、可下载的<b>试跑回执</b>。试跑批次与原文件导入回执、样本 ID 互相关联。
      </p>
    </div>
    <div class="claim-form">
      <label>
        <span>解析模板</span>
        <select v-model="templateId" data-testid="claim-template-select">
          <option value="">自动选择站点已启用模板 / 通用提取</option>
          <option v-for="t in templates" :key="t.id" :value="String(t.id)" data-testid="claim-template-option">
            #{{ t.id }} {{ t.模板名称 }}（适用：{{ t.适用站点 }} · {{ t.status }} · {{ t.解析版本 }}）
          </option>
        </select>
      </label>
      <label>
        <span>回归批次</span>
        <input v-model="batch" list="claim-batch-options" data-testid="claim-batch-input" placeholder="选择或输入样本批次" />
        <datalist id="claim-batch-options">
          <option v-for="b in batches" :key="b" :value="b" />
        </datalist>
      </label>
      <label v-if="!batch">
        <span>站点（可选）</span>
        <input v-model="site" data-testid="claim-site-input" placeholder="按站点过滤可领取样本" />
      </label>
      <button class="btn primary" type="button" data-testid="claim-run-btn" :disabled="running" @click="claimRun">
        {{ running ? '领取并提取中…' : '领取该批回归样本并试跑' }}
      </button>
      <button class="btn ghost" type="button" data-testid="reload-claimable-btn" @click="loadClaimable">刷新可领取批次</button>
      <p v-if="error" class="error-text" data-testid="claim-error">{{ error }}</p>
      <p v-if="claimInfo" class="ok-text" data-testid="claim-info">
        已从批次 {{ batch }} 领取 {{ claimInfo }} 份合格样本，价格字段为真实 HTML 提取结果。
      </p>
    </div>

    <div v-if="!trial" class="claimable-list" data-testid="claimable-list">
      <h4>当前可领取的回归样本（来自已验证导入行）</h4>
      <table class="data-table">
        <thead>
          <tr><th>样本ID</th><th>页面</th><th>批次</th><th>站点</th><th>关联导入回执</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in claimableSamples" :key="s.sample_id" :data-testid="`claimable-${s.page_code}`">
            <td>#{{ s.sample_id }}</td>
            <td><b>{{ s.page_code }}</b></td>
            <td>{{ s.batch }}</td>
            <td>{{ s.site }}</td>
            <td class="mono small">{{ s.import_no }}</td>
            <td>{{ s.status }}</td>
          </tr>
          <tr v-if="!claimableSamples.length">
            <td colspan="6" class="empty-state">暂无可领取样本，请先到「页面样本库」从历史文件导入回归材料。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <TrialWindow v-if="trial" :trial="trial" @updated="onUpdated" />
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  claimAndRun,
  getTrial,
  REG_SESSION_KEYS,
  type Trial,
} from '@/api/regression'
import type { EntryRow } from '@/data/types'
import TrialWindow from './TrialWindow.vue'

const templates = ref<EntryRow[]>([])
const batches = ref<string[]>([])
const claimableSamples = ref<
  { sample_id: number; page_code: string; batch: string; site: string; import_no: string; status: string }[]
>([])
const templateId = ref('')
const batch = ref('')
const site = ref('')
const running = ref(false)
const error = ref('')
const claimInfo = ref<number | null>(null)
const trial = ref<Trial | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  try {
    const page = await listEntries('page_parser', { size: '200' })
    templates.value = page.items
  } catch {
    templates.value = []
  }
  await loadClaimable()
  const savedBatch = sessionStorage.getItem(REG_SESSION_KEYS.batch)
  const savedTrial = sessionStorage.getItem(REG_SESSION_KEYS.trialNo)
  if (savedBatch) batch.value = savedBatch
  if (savedTrial) {
    try {
      trial.value = await getTrial(savedTrial)
      if (!batch.value) batch.value = trial.value.batch
      startPolling()
    } catch {
      /* 忽略失效的暂存试跑号 */
    }
  }
  window.addEventListener('parser:claim-template', onClaimTemplate as unknown as EventListener)
})

onBeforeUnmount(() => {
  window.removeEventListener('parser:claim-template', onClaimTemplate as unknown as EventListener)
  stopPolling()
})

function onClaimTemplate(event: CustomEvent<{ templateId?: number; batch?: string }>) {
  if (event.detail?.templateId) templateId.value = String(event.detail.templateId)
  if (event.detail?.batch) batch.value = event.detail.batch
  document.querySelector('[data-testid="parser-claim-panel"]')?.scrollIntoView({ behavior: 'smooth' })
}

async function loadClaimable() {
  try {
    const res = await fetch('/api/regression/claimable')
    const data = await res.json()
    batches.value = data.batches ?? []
    claimableSamples.value = data.samples ?? []
    if (!batch.value && batches.value.length) batch.value = batches.value[0]
  } catch {
    /* 后端未就绪时静默 */
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!trial.value || trial.value.status !== '等待补传') return stopPolling()
    try {
      trial.value = await getTrial(trial.value.trial_no)
    } catch {
      /* ignore */
    }
  }, 2500)
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
}
function onUpdated(next: Trial) {
  trial.value = next
  if (next.status === '等待补传') startPolling()
  else stopPolling()
}

async function claimRun() {
  running.value = true
  error.value = ''
  claimInfo.value = null
  try {
    if (!batch.value.trim()) throw new Error('请先选择或输入回归批次')
    const next = await claimAndRun({
      batch: batch.value.trim(),
      site: site.value.trim() || undefined,
      template_id: templateId.value || undefined,
    })
    trial.value = next
    claimInfo.value = next.valid_count
    sessionStorage.setItem(REG_SESSION_KEYS.trialNo, next.trial_no)
    sessionStorage.setItem(REG_SESSION_KEYS.batch, next.batch)
    await loadClaimable()
    startPolling()
    setTimeout(() => {
      document.querySelector('[data-testid="trial-window"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 80)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '领取试跑失败'
  } finally {
    running.value = false
  }
}
</script>

<style scoped>
.claim-panel { background: #fbfcfe; border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; margin: 12px 0 16px; }
.claim-head h3 { margin: 0 0 6px; font-size: 17px; }
.claim-head p { font-size: 12.5px; line-height: 1.8; margin: 0 0 12px; }
.claim-form { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
.claim-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: #475467; }
.claim-form select, .claim-form input { padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; min-width: 240px; }
.error-text { color: #b42318; font-size: 13px; }
.ok-text { color: #027a48; font-size: 13px; }
.muted { color: var(--muted); }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
.small { font-size: 12px; }
.claimable-list { margin-top: 12px; }
h4 { margin: 8px 0; font-size: 14px; }
</style>
