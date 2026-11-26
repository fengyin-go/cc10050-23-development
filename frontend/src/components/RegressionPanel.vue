<template>
  <section class="regression-panel" id="regression-entry" data-testid="regression-panel">
    <div class="rp-head">
      <h3>从历史文件准备回归</h3>
      <p class="rp-desc">
        选择<b>带站点和批次的历史页面文件</b>，或直接<b>上传回归材料</b>；先通过<b>导入回执</b>查看
        <b>逐行校验</b>与<b>后续动作</b>，再<b>从同一批启动模板试跑</b>。
        导入样本必须属于指定站点：<b>跨站同指纹不能覆盖原样本</b>；<b>损坏行单独说明</b>，
        回归模块不会把半份 HTML 当成已验证样本；<b>原导入请求重新上传同一页</b>时，回归报告只接收一份有资格的来源。
        导入回执与解析样本、试跑批次互相关联。
      </p>
    </div>

    <div class="rp-form" data-testid="import-form">
      <label class="rp-field">
        <span>指定站点（跨站行会被拒绝）</span>
        <input
          v-model="site"
          list="site-name-options"
          data-testid="target-site-input"
          placeholder="例如：云街甄选"
        />
        <datalist id="site-name-options">
          <option v-for="s in siteOptions" :key="s" :value="s" />
        </datalist>
      </label>
      <label class="rp-field small">
        <span>样本/回归批次</span>
        <input v-model="batch" data-testid="batch-input" placeholder="例如：RG-20261126-I1I2" />
      </label>
      <label class="rp-field grow">
        <span>选择历史页面文件（文件自带站点与批次）</span>
        <select v-model="selectedFileId" data-testid="history-file-select" @change="onSelectHistory">
          <option value="">— 请选择历史页面文件 —</option>
          <option v-for="f in historyFiles" :key="f.id" :value="f.id" data-testid="history-file-option">
            {{ f.file_name }} ｜ 站点：{{ f.site }} ｜ 批次：{{ f.batch }}
          </option>
        </select>
      </label>
      <label class="rp-field grow">
        <span>或上传本地回归材料（JSON / CSV / TXT / HTML）</span>
        <input
          type="file"
          data-testid="history-file-input"
          accept=".json,.jsonl,.csv,.tsv,.txt,.html,.htm,*/*"
          @change="onFileChosen"
        />
        <em v-if="pendingFileName" class="file-hint" data-testid="picked-file-name">已选择：{{ pendingFileName }}</em>
      </label>
      <details class="paste-box" data-testid="paste-box">
        <summary>或直接粘贴历史文件内容（JSON / CSV / 键值文本 / HTML 表格）</summary>
        <textarea
          v-model="pasteContent"
          rows="5"
          data-testid="paste-content"
          placeholder='{"lines":[{"页面编号":"I1","站点名称":"云街甄选","样本指纹":"fp1","价格":70,"html":"<span class=price>¥70</span>"}]}'
        />
        <button class="btn" type="button" data-testid="paste-import-btn" :disabled="importing" @click="submitPasted">
          用粘贴内容上传并校验
        </button>
      </details>
      <div class="rp-actions">
        <button class="btn primary" type="button" data-testid="upload-import-btn" :disabled="importing" @click="submitImport()">
          {{ importing ? '逐行校验中…' : '上传回归材料并查看导入回执' }}
        </button>
        <button class="btn" type="button" data-testid="scenario-import-btn" :disabled="importing" @click="runScenario70">
          一键演练：导入首传文件（I1=70 / I2半传 / 跨站同指纹行）
        </button>
        <button class="btn ghost" type="button" data-testid="download-template-btn" @click="downloadTemplate">
          下载导入模板(CSV)
        </button>
      </div>
      <p v-if="formError" class="error-text" data-testid="import-form-error">{{ formError }}</p>
    </div>

    <!-- 导入回执 -->
    <section
      v-if="receipt"
      class="receipt-card"
      data-testid="import-receipt"
      :data-import-no="receipt.import_no"
      :data-attempt="receipt.attempt_count"
    >
      <header class="receipt-head">
        <div>
          <h3>导入回执 <span class="mono" data-testid="receipt-no">{{ receipt.import_no }}</span>
            <em v-if="receipt.is_retry || receipt.attempt_count > 1" class="attempt-tag" data-testid="receipt-attempt">
              原导入请求 · 第 {{ receipt.attempt_count }} 次送达
            </em>
          </h3>
          <p class="muted small">{{ receipt.message }}</p>
          <p class="muted small">来源文件：{{ receipt.file_name }} ｜ 指定站点：{{ receipt.site || '按内容识别' }} ｜ 批次：{{ receipt.batch }}</p>
        </div>
        <div class="count-chips">
          <span class="chip ok">已验证 <b data-testid="count-valid">{{ receipt.counts.valid }}</b></span>
          <span class="chip reject">跨站拒绝 <b data-testid="count-cross">{{ receipt.counts.cross_site }}</b></span>
          <span class="chip broken">损坏/半传 <b data-testid="count-broken">{{ receipt.counts.broken }}</b></span>
          <span class="chip">合格来源 <b data-testid="count-eligible">{{ receipt.counts.eligible_sources }}</b></span>
        </div>
      </header>

      <!-- 逐行校验 -->
      <h4>逐行校验及后续动作</h4>
      <table class="data-table receipt-table" data-testid="receipt-lines">
        <thead>
          <tr>
            <th>行号</th><th>页面</th><th>申报站点</th><th>指纹</th><th>价格校验</th>
            <th>校验结果</th><th>原因 / 后续动作</th><th>关联样本</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="line in receipt.lines"
            :key="line.key"
            :data-testid="lineTestId(line)"
            :data-line-page="line.page_code"
            :data-verdict="line.verdict"
            :class="rowClass(line.verdict)"
          >
            <td>{{ line.line_no || '—' }}</td>
            <td><b>{{ line.page_code }}</b></td>
            <td :data-testid="`line-site-${line.line_no}`">{{ line.site || '—' }}</td>
            <td class="mono small">{{ line.fingerprint || '—' }}</td>
            <td>
              <span v-if="line.price != null" :data-testid="line.page_code === 'I1' && line.verdict === '已验证' ? 'verified-I1-price' : ''">
                <b>{{ formatPrice(line.price) }}</b>
              </span>
              <span v-else-if="line.partial_price != null" class="muted" title="半截金额，不可采信">
                片段 {{ formatPrice(line.partial_price) }}…（不采信）
              </span>
              <span v-else class="muted">—</span>
              <div v-if="line.first_price != null && line.price != null && Number(line.first_price) !== Number(line.price)"
                   class="price-correction" data-testid="price-correction">
                首传 {{ formatPrice(line.first_price) }} → 当前 <b>{{ formatPrice(line.price) }}</b>
              </div>
            </td>
            <td><span class="verdict-tag" :class="verdictClass(line.verdict)" :data-testid="`verdict-${line.page_code}-${line.line_no}`">{{ line.verdict }}</span></td>
            <td class="reason-cell">
              <p
                class="line-reason"
                :data-testid="line.verdict === '跨站拒绝' ? 'cross-site-reason' : line.page_code === 'I2' ? 'I2-receipt-detail' : 'line-reason'"
              >{{ line.reason }}</p>
              <p class="follow-up">➡ {{ line.follow_up }}</p>
            </td>
            <td class="mono small">
              <span v-if="line.sample_id != null" :data-testid="`line-sample-${line.page_code}`">
                <a href="javascript:void(0)" @click="scrollToSample(line.sample_id)">样本 #{{ line.sample_id }}</a>
              </span>
              <span v-else class="muted">未生成</span>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 无法解析的损坏行单独说明 -->
      <div v-if="receipt.parse_errors?.length" class="parse-errors" data-testid="corrupt-lines">
        <h4>损坏行单独说明（无法解析的原始行）</h4>
        <ul>
          <li v-for="(e, i) in receipt.parse_errors" :key="i">第 {{ e.line_no }} 行：{{ e.reason }}</li>
        </ul>
      </div>

      <!-- 重试原导入 + 启动试跑 -->
      <div class="receipt-actions">
        <button class="btn" type="button" data-testid="retry-import-btn" @click="showRetry = !showRetry">
          重试原导入（同一导入请求号 {{ receipt.import_no }}）
        </button>
        <button class="btn primary" type="button" data-testid="retry-correction-btn" :disabled="retrying" @click="retryWith79">
          {{ retrying ? '重传校验中…' : '用更正文件重传（I1 价格 70 → 79，I2 仍补传）' }}
        </button>
        <button class="btn primary big" type="button" data-testid="start-trial-btn" :disabled="starting" @click="startTrial">
          {{ starting ? '正在领取样本并试跑…' : '从同一批启动模板试跑（在同批试跑窗口提交回归）' }}
        </button>
      </div>

      <div v-if="showRetry" class="retry-box" data-testid="retry-box">
        <label>
          <span>选择重传的历史文件</span>
          <select v-model="retryFileId" data-testid="retry-history-select">
            <option value="">— 不使用历史文件 —</option>
            <option v-for="f in historyFiles" :key="f.id" :value="f.id">{{ f.file_name }}</option>
          </select>
        </label>
        <label>
          <span>或选择更正后的本地文件（同一页 I1）</span>
          <input type="file" data-testid="retry-file-input" accept=".json,.jsonl,.csv,.tsv,.txt,.html,.htm,*/*" @change="onRetryFileChosen" />
          <em v-if="retryFileName" class="file-hint">{{ retryFileName }}</em>
        </label>
        <button class="btn primary" type="button" data-testid="submit-retry-btn" :disabled="retrying" @click="submitRetry()">
          提交重传（仍归属原导入请求 {{ receipt.import_no }}）
        </button>
        <p v-if="retryError" class="error-text">{{ retryError }}</p>
      </div>

      <!-- 送达记录 -->
      <details class="attempts" data-testid="attempt-history" open>
        <summary>送达 / 重传记录（{{ receipt.attempts.length }}）</summary>
        <ol>
          <li v-for="a in receipt.attempts" :key="a.attempt">
            第 {{ a.attempt }} 次 · {{ a.file_name }} · 合格 {{ a.valid }} / 跨站 {{ a.cross_site }} / 损坏 {{ a.broken }}
            <span v-if="a.price_by_page && Object.keys(a.price_by_page).length" class="mono small">
              ｜价格 {{ JSON.stringify(a.price_by_page) }}
            </span>
          </li>
        </ol>
      </details>
    </section>

    <p v-if="!receipt && !formError" class="muted rp-tip">
      还没有导入回执。可选择上方演练文件，或准备包含「页面编号 / 站点 / 指纹 / 价格 / HTML / 传输状态」列的文件后上传。
    </p>

    <TrialWindow v-if="trial" :trial="trial" @updated="onTrialUpdated" />
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  createImport,
  createTrial,
  getImport,
  getTrial,
  listHistoryFiles,
  readHistoryFile,
  REG_SESSION_KEYS,
  retryImport,
  type HistoryFile,
  type ImportReceipt,
  type ReceiptLine,
  type Trial,
} from '@/api/regression'
import TrialWindow from './TrialWindow.vue'

const emit = defineEmits<{ (e: 'changed'): void }>()

const historyFiles = ref<HistoryFile[]>([])
const siteOptions = ref<string[]>([])
const site = ref('')
const batch = ref('')
const selectedFileId = ref('')
const pendingContent = ref('')
const pendingFileName = ref('')
const pasteContent = ref('')

const importing = ref(false)
const starting = ref(false)
const retrying = ref(false)
const formError = ref('')
const retryError = ref('')

const receipt = ref<ImportReceipt | null>(null)
const trial = ref<Trial | null>(null)

const showRetry = ref(false)
const retryFileId = ref('HF-SCENARIO-79')
const retryContent = ref('')
const retryFileName = ref('')

let pollTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  try {
    historyFiles.value = await listHistoryFiles()
    const sitePage = await listEntries('shop_site', {})
    siteOptions.value = sitePage.items.map((r) => String(r['站点名称'] ?? '')).filter(Boolean)
  } catch {
    /* 站点候选加载失败不阻塞主流程 */
  }
  const savedImport = sessionStorage.getItem(REG_SESSION_KEYS.importNo)
  const savedTrial = sessionStorage.getItem(REG_SESSION_KEYS.trialNo)
  const savedBatch = sessionStorage.getItem(REG_SESSION_KEYS.batch)
  if (savedBatch) batch.value = savedBatch
  if (savedImport) {
    try {
      receipt.value = await getImport(savedImport)
      site.value = receipt.value.site
      batch.value = receipt.value.batch
    } catch {
      sessionStorage.removeItem(REG_SESSION_KEYS.importNo)
    }
  }
  if (savedTrial) {
    try {
      trial.value = await getTrial(savedTrial)
      startPolling()
    } catch {
      sessionStorage.removeItem(REG_SESSION_KEYS.trialNo)
    }
  }
  window.addEventListener('regression:open-receipt', onOpenReceipt as unknown as EventListener)
})

onBeforeUnmount(() => {
  stopPolling()
  window.removeEventListener('regression:open-receipt', onOpenReceipt as unknown as EventListener)
})

async function onOpenReceipt(event: CustomEvent<{ importNo?: string }>) {
  const importNo = event.detail?.importNo
  if (!importNo) return
  try {
    receipt.value = await getImport(importNo)
    site.value = receipt.value.site
    batch.value = receipt.value.batch
    sessionStorage.setItem(REG_SESSION_KEYS.importNo, importNo)
  } catch {
    /* 回执不存在时忽略 */
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!trial.value || trial.value.status !== '等待补传') {
      stopPolling()
      return
    }
    try {
      trial.value = await getTrial(trial.value.trial_no)
    } catch {
      /* 轮询失败忽略，下次再试 */
    }
  }, 2500)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function onTrialUpdated(next: Trial) {
  trial.value = next
  if (next.status === '等待补传') startPolling()
  else stopPolling()
}

function onSelectHistory() {
  const f = historyFiles.value.find((x) => x.id === selectedFileId.value)
  if (f) {
    site.value = f.site
    batch.value = f.batch
    pendingContent.value = ''
    pendingFileName.value = ''
  }
}

async function onFileChosen(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  pendingContent.value = await readHistoryFile(file)
  pendingFileName.value = file.name
  selectedFileId.value = ''
  // 尝试从内容里预填站点/批次
  try {
    const parsed = JSON.parse(pendingContent.value)
    site.value = site.value || parsed.站点名称 || parsed.site || ''
    batch.value = batch.value || parsed.样本批次 || parsed.batch || ''
  } catch {
    /* 非 JSON 时保留手工输入 */
  }
}

async function onRetryFileChosen(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  retryContent.value = await readHistoryFile(file)
  retryFileName.value = file.name
  retryFileId.value = ''
}

async function submitImport(forceHistoryId?: string) {
  formError.value = ''
  importing.value = true
  try {
    const historyId = forceHistoryId ?? (pendingContent.value ? '' : selectedFileId.value)
    if (!historyId && !pendingContent.value) {
      throw new Error('请先选择一份历史页面文件，或选择本地文件上传')
    }
    const created = await createImport({
      site: site.value.trim(),
      batch: batch.value.trim(),
      history_file_id: historyId || undefined,
      content: pendingContent.value || undefined,
      file_name: pendingFileName.value || undefined,
    })
    if (!created.ok) throw new Error(created.message || '导入失败')
    receipt.value = created
    site.value = created.site
    batch.value = created.batch
    sessionStorage.setItem(REG_SESSION_KEYS.importNo, created.import_no)
    sessionStorage.setItem(REG_SESSION_KEYS.batch, created.batch)
    // 若已有试跑窗口，后端已自动同步；这里拉回最新结果
    if (trial.value && created.synced_trials?.includes(trial.value.trial_no)) {
      trial.value = await getTrial(trial.value.trial_no)
    }
    emit('changed')
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '导入失败'
  } finally {
    importing.value = false
  }
}

async function submitPasted() {
  if (!pasteContent.value.trim()) {
    formError.value = '请先粘贴历史文件内容'
    return
  }
  pendingContent.value = pasteContent.value
  pendingFileName.value = 'pasted-history.txt'
  selectedFileId.value = ''
  await submitImport('')
}

async function runScenario70() {
  selectedFileId.value = 'HF-SCENARIO-70'
  const f = historyFiles.value.find((x) => x.id === 'HF-SCENARIO-70')
  if (f) {
    site.value = f.site
    batch.value = f.batch
  }
  pendingContent.value = ''
  pendingFileName.value = ''
  await submitImport('HF-SCENARIO-70')
}

async function retryWith79() {
  if (!receipt.value) return
  retryFileId.value = 'HF-SCENARIO-79'
  retryContent.value = ''
  retryFileName.value = ''
  await submitRetry('HF-SCENARIO-79')
}

async function submitRetry(forceHistoryId?: string) {
  if (!receipt.value) return
  retryError.value = ''
  retrying.value = true
  try {
    const historyId = forceHistoryId ?? (retryContent.value ? '' : retryFileId.value)
    if (!historyId && !retryContent.value) {
      throw new Error('请选择更正后的历史文件或本地文件')
    }
    const updated = await retryImport(receipt.value.import_no, {
      site: site.value.trim(),
      batch: batch.value.trim(),
      history_file_id: historyId || undefined,
      content: retryContent.value || undefined,
      file_name: retryFileName.value || undefined,
    })
    receipt.value = updated
    sessionStorage.setItem(REG_SESSION_KEYS.importNo, updated.import_no)
    showRetry.value = false
    if (trial.value) {
      trial.value = await getTrial(trial.value.trial_no)
      startPolling()
    }
    emit('changed')
  } catch (error) {
    retryError.value = error instanceof Error ? error.message : '重传失败'
  } finally {
    retrying.value = false
  }
}

async function startTrial() {
  starting.value = true
  formError.value = ''
  try {
    const next = await createTrial({
      import_no: receipt.value?.import_no,
      batch: batch.value || receipt.value?.batch,
      site: site.value || receipt.value?.site,
    })
    trial.value = next
    sessionStorage.setItem(REG_SESSION_KEYS.trialNo, next.trial_no)
    if (next.status === '等待补传') startPolling()
    emit('changed')
    setTimeout(() => {
      document.querySelector('[data-testid="trial-window"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 80)
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '启动试跑失败'
  } finally {
    starting.value = false
  }
}

function scrollToSample(id?: number | null) {
  if (id == null) return
  document.querySelector(`[data-testid="row-${id}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function formatPrice(value: number | null | undefined): string {
  if (value == null) return '—'
  return Number.isInteger(Number(value)) ? String(Number(value)) : Number(value).toFixed(2)
}

function lineTestId(line: ReceiptLine): string {
  if (line.verdict === '已验证') return `receipt-line-${line.page_code}`
  if (line.verdict === '跨站拒绝') return `receipt-line-${line.page_code}-cross`
  if (line.verdict === '损坏') return `receipt-line-${line.page_code}-broken`
  return `receipt-line-${line.line_no}`
}

function rowClass(verdict: string): string {
  return {
    已验证: 'row-valid',
    跨站拒绝: 'row-cross',
    损坏: 'row-broken',
    无法解析: 'row-broken',
    重复来源: 'row-dup',
  }[verdict] ?? ''
}

function verdictClass(verdict: string): string {
  return {
    已验证: 'v-ok',
    跨站拒绝: 'v-cross',
    损坏: 'v-broken',
    无法解析: 'v-broken',
    重复来源: 'v-dup',
  }[verdict] ?? ''
}

function downloadTemplate() {
  const header = '页面编号,站点名称,样本批次,样本指纹,价格,传输状态,备注,html'
  const example = [
    'I1', '云街甄选', 'RG-DEMO', 'fp-demo-1', '79', '完整', '',
    '"<html><body><span class=""price"">¥79.00</span></body></html>"',
  ].join(',')
  const blob = new Blob(['﻿' + header + '\n' + example + '\n'], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '历史页面导入模板.csv'
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<style scoped>
.regression-panel {
  background: linear-gradient(180deg, #f8fbff 0%, #fff 100%);
  border: 1px solid #b2ddff;
  border-radius: 12px;
  padding: 16px 18px;
  margin: 12px 0 16px;
}
.rp-head h3 { margin: 0 0 6px; font-size: 17px; }
.rp-desc { font-size: 12.5px; color: #475467; line-height: 1.8; margin: 0 0 12px; }
.rp-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
.rp-field { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: #475467; }
.rp-field input, .rp-field select { padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; min-width: 200px; }
.rp-field.small input { min-width: 170px; }
.rp-field.grow { flex: 1; min-width: 260px; }
.rp-actions { display: flex; gap: 8px; flex-wrap: wrap; width: 100%; }
.file-hint { color: var(--brand); font-style: normal; font-size: 12px; }
.paste-box { flex-basis: 100%; font-size: 12.5px; }
.paste-box textarea { width: 100%; margin: 6px 0; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.error-text { color: #b42318; font-size: 13px; width: 100%; }
.rp-tip { color: var(--muted); font-size: 13px; }
.receipt-card { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-top: 14px; }
.receipt-head { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.receipt-head h3 { margin: 0; font-size: 16px; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.attempt-tag { font-style: normal; font-size: 12px; background: #eff8ff; color: #1849a9; border: 1px solid #b2ddff; border-radius: 4px; padding: 1px 8px; margin-left: 6px; }
.count-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { border: 1px solid var(--border); background: #f8fafc; border-radius: 999px; padding: 4px 12px; font-size: 12.5px; }
.chip.ok { background: #ecfdf3; border-color: #6ce9a6; }
.chip.reject { background: #fef3f2; border-color: #fda29b; }
.chip.broken { background: #fffaeb; border-color: #fec84b; }
.receipt-table { margin-top: 8px; }
.receipt-table td, .receipt-table th { vertical-align: top; }
.row-valid { background: #f6fef9; }
.row-cross { background: #fff7f6; }
.row-broken { background: #fffcF5; }
.row-dup { background: #f8fafc; }
.verdict-tag { border-radius: 4px; padding: 2px 8px; color: #fff; font-size: 12px; white-space: nowrap; }
.v-ok { background: #027a48; }
.v-cross { background: #d92d20; }
.v-broken { background: #dc6803; }
.v-dup { background: #667085; }
.reason-cell { max-width: 420px; }
.line-reason { margin: 0; font-size: 12.5px; line-height: 1.6; }
.follow-up { margin: 4px 0 0; font-size: 12px; color: #475467; }
.price-correction { font-size: 12px; color: #b54708; margin-top: 2px; }
.parse-errors { margin-top: 10px; background: #fef3f2; border: 1px solid #fecdca; border-radius: 8px; padding: 8px 12px; font-size: 13px; color: #912018; }
.parse-errors h4 { margin: 4px 0; }
.receipt-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; align-items: center; }
.btn.big { padding: 9px 18px; font-size: 14px; }
.retry-box { display: flex; gap: 14px; align-items: flex-end; flex-wrap: wrap; background: #f8fafc; border: 1px dashed var(--border); border-radius: 8px; padding: 12px; margin-top: 12px; }
.retry-box label { display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; }
.retry-box select, .retry-box input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; min-width: 240px; }
.attempts { margin-top: 12px; font-size: 12.5px; color: #475467; }
.attempts ol { margin: 6px 0; padding-left: 20px; }
h4 { font-size: 14px; margin: 12px 0 6px; }
</style>
