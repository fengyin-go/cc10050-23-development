<template>
  <section
    v-if="trial"
    class="trial-window"
    data-testid="trial-window"
    :data-trial-no="trial.trial_no"
    :data-status="trial.status"
  >
    <header class="rw-head">
      <div>
        <h3>同批模板试跑窗口 · 回归批次
          <span class="mono" data-testid="trial-no">{{ trial.trial_no }}</span>
        </h3>
        <p class="rw-sub">
          批次 <b data-testid="trial-batch">{{ trial.batch || '—' }}</b>
          ｜站点 <b>{{ trial.site || '—' }}</b>
          ｜原导入回执
          <b class="mono" data-testid="trial-import-no">{{ trial.import_no || '—' }}</b>
          ｜解析模板 <b>{{ trial.template_name }}</b>（{{ trial.template_version }}）
        </p>
      </div>
      <span class="status-badge" :class="trial.status === '已完成' ? 'ok' : 'waiting'" data-testid="trial-status">
        {{ trial.status }}
      </span>
    </header>

    <!-- 受理结论：明确回答“继续处理 / 等待补传 / 是否需要重新受理” -->
    <div class="acceptance" data-testid="acceptance-conclusion">
      <template v-if="trial.status === '等待补传'">
        ✅ 已提交的回归<b>继续处理</b>：合格来源已用模板真实提取；I1 更正会在<b>同一试跑批次</b>自动跟进，
        <b>无需重新受理</b>。当前因 I2 半份 HTML <b>等待补传</b>，补传后点「刷新试跑结果」即可并入同一批次。
        如审核规则要求材料变更后重新立案，也可点「重新受理试跑」（沿用同一批次号与下载凭据）。
      </template>
      <template v-else>
        🎉 本批材料已齐备，试跑<b>继续处理并完成</b>，无需重新受理；如下发了更正，可刷新核对最新采用值。
      </template>
    </div>

    <div class="rw-toolbar">
      <span class="credential">
        下载凭据：
        <code data-testid="download-credential">{{ trial.credential }}</code>
        <button class="btn mini" type="button" data-testid="copy-credential" @click="copyCredential">复制</button>
      </span>
      <span class="spacer" />
      <button class="btn" type="button" data-testid="refresh-trial-btn" :disabled="busy" @click="onRefresh">
        {{ busy ? '刷新中…' : '刷新试跑结果（保持窗口）' }}
      </button>
      <button class="btn ghost" type="button" data-testid="reaccept-trial-btn" :disabled="busy" @click="onReaccept">
        重新受理试跑
      </button>
      <a
        class="btn primary"
        data-testid="download-receipt-btn"
        :href="csvUrl"
        :download="`试跑回执_${trial.trial_no}.csv`"
      >下载试跑回执(CSV)</a>
      <a
        class="btn"
        data-testid="download-json-btn"
        :href="jsonUrl"
        :download="`试跑回执_${trial.trial_no}.json`"
      >JSON回执</a>
    </div>
    <p v-if="actionError" class="error-text" data-testid="trial-action-error">{{ actionError }}</p>
    <p v-if="copied" class="ok-text">凭据已复制到剪贴板。</p>

    <div class="metric-chips">
      <span class="chip" data-testid="valid-count">有效记录 <b>{{ trial.valid_count }}</b> 条</span>
      <span class="chip" data-testid="computed-pages">计算页面 <b>{{ trial.computed_pages.join('、') || '无' }}</b></span>
      <span class="chip price-chip" data-testid="trial-adopted-price">
        采用的 I1 价格 <b>{{ adoptedPriceText }}</b>
      </span>
      <span class="chip reject-chip">跨站剔除 <b>{{ trial.rejected.length }}</b> 行</span>
      <span class="chip pending-chip">等待补传 <b>{{ trial.pending.length }}</b> 页</span>
    </div>

    <h4>解析字段（模板领取样本后真实提取）</h4>
    <table class="data-table rw-table" data-testid="trial-results">
      <thead>
        <tr>
          <th>页面</th><th>关联样本ID</th><th>商品标题</th><th>价格字段</th><th>币种</th>
          <th>提取策略/选择器</th><th>指纹</th><th>关联导入回执</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="r in trial.results"
          :key="r.page_code"
          :data-testid="`trial-result-${r.page_code}`"
          :data-page="r.page_code"
          class="result-row"
        >
          <td><b>{{ r.page_code }}</b></td>
          <td :data-testid="`result-sample-${r.page_code}`">#{{ r.sample_id ?? '—' }}</td>
          <td>{{ r.title || '—' }}</td>
          <td>
            <b class="price-value" :data-testid="`result-price-${r.page_code}`">{{ r.price_display }}</b>
            <span class="muted">（{{ r.price_text || '未提取到金额' }}）</span>
            <code
              v-if="r.parsed_fields"
              class="parsed-fields"
              :data-testid="`parsed-fields-${r.page_code}`"
            >{{ JSON.stringify(r.parsed_fields) }}</code>
          </td>
          <td>{{ r.currency }}</td>
          <td class="mono small">{{ r.extractor }}<span v-if="r.selector_used"> ｜ {{ r.selector_used }}</span></td>
          <td class="mono small">{{ r.fingerprint }}</td>
          <td class="mono small">{{ r.import_no }}</td>
        </tr>
        <tr v-if="!trial.results.length">
          <td colspan="8" class="empty-state">当前还没有可计算的合格来源（半份 HTML 与跨站行均不会进入这里）。</td>
        </tr>
      </tbody>
    </table>

    <h4>跨站试样（行内拒绝原因，已从回归计算剔除）</h4>
    <div
      v-for="(r, idx) in trial.rejected"
      :key="`rej-${r.page_code}-${idx}`"
      class="inline-reject"
      :data-testid="`reject-${r.page_code}-${r.line_no ?? idx}`"
      :data-page="r.page_code"
    >
      <div class="reject-head">
        <span class="tag reject">跨站拒绝</span>
        <b>{{ r.page_code }}</b>
        <span>申报站点：{{ r.site || '—' }}</span>
        <span class="mono small">指纹：{{ r.fingerprint || '—' }}</span>
      </div>
      <p class="inline-reason" data-testid="inline-reject-reason">🚫 {{ r.reason }}</p>
      <p class="follow-up">后续动作：{{ r.follow_up }}</p>
    </div>
    <p v-if="!trial.rejected.length" class="muted small">无跨站拒绝行。</p>

    <h4>损坏 / 待补传页面详情（不计入回归结果）</h4>
    <div
      v-for="(p, idx) in trial.pending"
      :key="`pend-${p.page_code}-${idx}`"
      class="inline-pending"
      :data-testid="`pending-${p.page_code}-${p.line_no ?? idx}`"
      :data-page="p.page_code"
    >
      <div class="reject-head">
        <span class="tag pending">等待补传</span>
        <b :data-testid="`pending-page-${p.page_code}`">{{ p.page_code }}</b>
        <span class="mono small">指纹：{{ p.fingerprint || '—' }}</span>
      </div>
      <p class="pending-reason" :data-testid="p.page_code === 'I2' ? 'i2-detail' : 'pending-detail'">⚠️ {{ p.reason }}</p>
      <ul v-if="p.completeness_detail?.length" class="detail-list">
        <li v-for="(d, i) in p.completeness_detail" :key="i">{{ d }}</li>
      </ul>
      <p class="follow-up">后续动作：{{ p.follow_up }}</p>
    </div>
    <p v-if="!trial.pending.length" class="muted small">没有待补传页面。</p>

    <h4>受理与更正轨迹</h4>
    <ul class="event-log" data-testid="trial-events">
      <li v-for="(e, i) in trial.events" :key="i" :data-kind="e.kind">
        <span class="muted small">{{ formatTime(e.at) }}</span> {{ e.text }}
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  receiptDownloadUrl,
  refreshTrial,
  reacceptTrial,
  type Trial,
} from '@/api/regression'

const props = defineProps<{ trial: Trial }>()
const emit = defineEmits<{ (e: 'updated', trial: Trial): void }>()

const busy = ref(false)
const actionError = ref('')
const copied = ref(false)

const apiBase = import.meta.env.VITE_API_BASE ?? ''
const csvUrl = computed(() => apiBase + receiptDownloadUrl(props.trial.trial_no, props.trial.credential, 'csv'))
const jsonUrl = computed(() => apiBase + receiptDownloadUrl(props.trial.trial_no, props.trial.credential, 'json'))
const adoptedPriceText = computed(() => {
  const p = props.trial.adopted_i1_price ?? props.trial.adopted_price
  return p === null || p === undefined ? '暂无（I1 尚未合格）' : `${Number(p).toFixed(0)}（${p}）`
})

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

async function onRefresh() {
  busy.value = true
  actionError.value = ''
  try {
    emit('updated', await refreshTrial(props.trial.trial_no))
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '刷新失败'
  } finally {
    busy.value = false
  }
}

async function onReaccept() {
  busy.value = true
  actionError.value = ''
  try {
    emit('updated', await reacceptTrial(props.trial.trial_no))
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '重新受理失败'
  } finally {
    busy.value = false
  }
}

async function copyCredential() {
  try {
    await navigator.clipboard.writeText(props.trial.credential)
    copied.value = true
    setTimeout(() => (copied.value = false), 1800)
  } catch {
    copied.value = false
  }
}
</script>

<style scoped>
.trial-window {
  background: #fff;
  border: 2px solid #1f6feb33;
  border-radius: 12px;
  padding: 16px;
  margin: 14px 0;
  box-shadow: 0 10px 30px rgb(31 111 235 / 8%);
}
.rw-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.rw-head h3 { margin: 0; font-size: 16px; }
.rw-sub { color: var(--muted); font-size: 12.5px; margin: 6px 0 0; }
.mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
.small { font-size: 12px; }
.muted { color: var(--muted); }
.status-badge { border-radius: 999px; padding: 4px 12px; font-size: 13px; white-space: nowrap; }
.status-badge.waiting { background: #fff4e5; color: #b54708; border: 1px solid #fdb022; }
.status-badge.ok { background: #ecfdf3; color: #027a48; border: 1px solid #6ce9a6; }
.acceptance {
  background: #eff8ff; border: 1px solid #b2ddff; color: #1849a9;
  border-radius: 8px; padding: 10px 12px; font-size: 13px; line-height: 1.7; margin: 12px 0;
}
.rw-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.rw-toolbar .spacer { flex: 1; }
.credential { font-size: 12.5px; color: #475467; }
.credential code { background: #f2f4f7; border: 1px solid var(--border); border-radius: 4px; padding: 2px 6px; }
.btn.mini { padding: 2px 8px; font-size: 12px; }
.metric-chips { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 14px; }
.chip { background: #f8fafc; border: 1px solid var(--border); border-radius: 999px; padding: 4px 12px; font-size: 13px; }
.chip b { color: #1f2937; }
.price-chip { background: #ecfdf3; border-color: #6ce9a6; }
.reject-chip { background: #fef3f2; border-color: #fda29b; }
.pending-chip { background: #fffaeb; border-color: #fec84b; }
.rw-table { margin-bottom: 14px; }
.price-value { color: #027a48; font-size: 15px; }
.parsed-fields { display: block; margin-top: 4px; font-size: 11px; color: #344054; background: #f2f4f7; border-radius: 4px; padding: 3px 6px; word-break: break-all; white-space: normal; }
.inline-reject, .inline-pending {
  border-radius: 8px; padding: 10px 12px; margin: 8px 0; font-size: 13px; line-height: 1.6;
}
.inline-reject { background: #fef3f2; border: 1px solid #fecdca; }
.inline-pending { background: #fffaeb; border: 1px solid #fedf89; }
.reject-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.tag { border-radius: 4px; padding: 1px 8px; font-size: 12px; color: #fff; }
.tag.reject { background: #d92d20; }
.tag.pending { background: #dc6803; }
.inline-reason { color: #912018; margin: 6px 0; }
.pending-reason { color: #93370d; margin: 6px 0; }
.follow-up { color: #475467; margin: 4px 0 0; font-size: 12.5px; }
.detail-list { margin: 4px 0; padding-left: 20px; color: #b54708; font-size: 12.5px; }
.event-log { list-style: none; padding: 0; margin: 0; border-left: 2px solid #d0d5dd; }
.event-log li { padding: 4px 0 8px 12px; font-size: 12.5px; line-height: 1.6; position: relative; }
.event-log li::before {
  content: ''; width: 8px; height: 8px; border-radius: 50%; background: var(--brand);
  position: absolute; left: -5px; top: 10px;
}
.error-text { color: #b42318; font-size: 13px; }
.ok-text { color: #027a48; font-size: 12.5px; }
h4 { margin: 14px 0 6px; font-size: 14px; }
</style>
