/** 历史文件导入回执 + 同批模板试跑的 API 封装。
 *  后端同时提供 /api/regression/* 与 /api/sample_store/* 别名，这里统一用回归语义路径。
 */
import { request } from './client'

export type ReceiptLine = {
  key: string
  line_no: number
  page_code: string
  site: string
  target_site?: string
  batch?: string
  fingerprint?: string
  declared_price?: number | null
  partial_price?: number | null
  price?: number | null
  price_text?: string
  title?: string
  verdict: '已验证' | '跨站拒绝' | '损坏' | '重复来源' | '无法解析' | string
  eligible: boolean
  reason: string
  follow_up: string
  sample_id?: number | null
  first_price?: number | null
  price_history?: { at: string; price: number; file_name?: string }[]
  completeness_detail?: string[]
}

export type ImportReceipt = {
  ok: boolean
  import_no: string
  import_request_no: string
  is_retry?: boolean
  retry_of?: string
  site: string
  batch: string
  file_name: string
  attempt_count: number
  attempts: { attempt: number; at: string; file_name: string; [k: string]: unknown }[]
  lines: ReceiptLine[]
  parse_errors: { line_no: number; reason: string }[]
  sample_ids: number[]
  counts: {
    total: number
    valid: number
    cross_site: number
    broken: number
    duplicate: number
    eligible_sources: number
  }
  message: string
  synced_trials?: string[]
}

export type TrialResult = {
  page_code: string
  sample_id: number | null
  import_no: string
  site: string
  fingerprint: string
  title: string
  price: number | null
  price_display: string
  price_text: string
  currency: string
  extractor: string
  selector_used?: string
  candidates?: string[]
  status: string
  parsed_fields?: Record<string, string | number | null>
  fields?: Record<string, string | number | null>
}

export type TrialOutcome = {
  line_no?: number
  page_code: string
  site?: string
  fingerprint?: string
  sample_id?: number | null
  reason: string
  follow_up: string
  verdict?: string
  completeness_detail?: string[]
}

export type Trial = {
  id: number
  trial_no: string
  regression_batch: string
  batch: string
  site: string
  import_no: string
  template_id?: number
  template_name: string
  template_version: string
  credential: string
  status: '等待补传' | '已完成' | '排队中' | string
  acceptance: string
  needs_reacceptance: boolean
  continue_processing: boolean
  created_at: string
  updated_at: string
  results: TrialResult[]
  rejected: TrialOutcome[]
  pending: TrialOutcome[]
  events: { at: string; kind: string; text: string }[]
  message: string
  valid_count: number
  computed_pages: string[]
  adopted_i1_price?: number | null
  adopted_price?: number | null
  cross_site_count?: number
  pending_count?: number
}

export type HistoryFile = {
  id: string
  file_name: string
  site: string
  batch: string
  note: string
  size: number
  updated_at: string
  builtin: boolean
  content?: string
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = ''
    try {
      detail = (await response.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(detail || `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function listHistoryFiles(): Promise<HistoryFile[]> {
  const res = await request('/api/regression/history-files')
  const data = await asJson<{ items: HistoryFile[] }>(res)
  return data.items
}

export async function getHistoryFile(id: string): Promise<HistoryFile> {
  const res = await request(`/api/regression/history-files/${encodeURIComponent(id)}`)
  const data = await asJson<{ item: HistoryFile }>(res)
  return data.item
}

export type ImportPayload = {
  site?: string
  batch?: string
  import_no?: string
  history_file_id?: string
  file_name?: string
  content?: string
}

export async function createImport(payload: ImportPayload): Promise<ImportReceipt> {
  const res = await request('/api/regression/imports', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return asJson<ImportReceipt>(res)
}

export async function retryImport(importNo: string, payload: ImportPayload): Promise<ImportReceipt> {
  const res = await request(`/api/regression/imports/${encodeURIComponent(importNo)}/retry`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return asJson<ImportReceipt>(res)
}

export async function getImport(importNo: string): Promise<ImportReceipt> {
  const res = await request(`/api/regression/imports/${encodeURIComponent(importNo)}`)
  return asJson<ImportReceipt>(res)
}

export async function createTrial(payload: {
  batch?: string
  site?: string
  import_no?: string
  template_id?: number | string
}): Promise<Trial> {
  const res = await request('/api/regression/trials', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return asJson<Trial>(res)
}

export async function getTrial(trialNo: string): Promise<Trial> {
  const res = await request(`/api/regression/trials/${encodeURIComponent(trialNo)}`)
  return asJson<Trial>(res)
}

export async function refreshTrial(trialNo: string): Promise<Trial> {
  const res = await request(`/api/regression/trials/${encodeURIComponent(trialNo)}/refresh`, {
    method: 'POST',
  })
  return asJson<Trial>(res)
}

export async function reacceptTrial(trialNo: string): Promise<Trial> {
  const res = await request(`/api/regression/trials/${encodeURIComponent(trialNo)}/reaccept`, {
    method: 'POST',
  })
  return asJson<Trial>(res)
}

export function receiptDownloadUrl(trialNo: string, credential: string, format: 'csv' | 'json' = 'csv'): string {
  return `/api/regression/trials/${encodeURIComponent(trialNo)}/receipt?credential=${encodeURIComponent(
    credential,
  )}&format=${format}`
}

export async function claimAndRun(payload: {
  batch?: string
  site?: string
  template_id?: number | string
}): Promise<Trial> {
  const res = await request('/api/page_parser/regression/claim', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return asJson<Trial>(res)
}

/** 浏览器端读取上传文件文本，UTF-8 失败时回退 GBK，避免中文站点名乱码。 */
export async function readHistoryFile(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buffer)
  } catch {
    try {
      return new TextDecoder('gbk').decode(buffer)
    } catch {
      return new TextDecoder('utf-8').decode(buffer)
    }
  }
}

export const REG_SESSION_KEYS = {
  importNo: 'regression.importNo',
  trialNo: 'regression.trialNo',
  batch: 'regression.batch',
}
