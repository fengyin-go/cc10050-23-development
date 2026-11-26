<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button
          v-if="meta.key === 'sample_store'"
          class="btn"
          data-testid="open-regression"
          type="button"
          @click="scrollToRegression"
        >
          从历史文件准备回归
        </button>
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <RegressionPanel v-if="meta.key === 'sample_store'" @changed="reload" />
    <ParserClaimPanel v-if="meta.key === 'page_parser'" />

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th v-if="hasLinkColumn">回归关联</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td v-if="hasLinkColumn" class="link-cell" data-testid="row-linkage">
            <template v-if="meta.key === 'sample_store'">
              <span v-if="row['来源导入回执']" class="mono small" :data-testid="`row-receipt-${row.id}`">
                回执 {{ row['来源导入回执'] }}
              </span>
              <span v-else class="muted">—</span>
              <span v-if="row['最近试跑批次']" class="mono small">｜试跑 {{ row['最近试跑批次'] }}</span>
            </template>
            <template v-else-if="meta.key === 'page_parser'">
              <button class="link" type="button" :data-testid="`claim-row-${row.id}`" @click="claimFromRow(row)">
                领取该模板回归样本
              </button>
            </template>
          </td>
          <td class="row-actions">
            <button
              v-if="meta.key === 'sample_store' && row['来源导入回执']"
              class="link"
              type="button"
              :data-testid="`receipt-row-${row.id}`"
              @click="openReceipt(row)"
            >
              查看导入回执
            </button>
            <button
              v-if="meta.key === 'page_parser'"
              class="link"
              type="button"
              :data-testid="`claim-action-${row.id}`"
              @click="claimFromRow(row)"
            >
              领取回归样本
            </button>
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2 + (hasLinkColumn ? 1 : 0)" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import ParserClaimPanel from '@/components/ParserClaimPanel.vue'
import RegressionPanel from '@/components/RegressionPanel.vue'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const hasLinkColumn = computed(() => meta.key === 'sample_store' || meta.key === 'page_parser')
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

function scrollToRegression() {
  document.querySelector('[data-testid="regression-panel"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function openReceipt(row: EntryRow) {
  const importNo = String(row['来源导入回执'] ?? '')
  if (importNo) {
    sessionStorage.setItem('regression.importNo', importNo)
    window.dispatchEvent(new CustomEvent('regression:open-receipt', { detail: { importNo } }))
  }
  scrollToRegression()
}

function claimFromRow(row: EntryRow) {
  const batch = String(row['样本批次'] ?? '')
  window.dispatchEvent(
    new CustomEvent('parser:claim-template', {
      detail: { templateId: Number(row.id), batch: batch.includes('样例') ? '' : batch },
    }),
  )
  document.querySelector('[data-testid="parser-claim-panel"]')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
