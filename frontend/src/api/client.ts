/** API 客户端：fetch 封装 + 类型定义 + JWT 注入 */

export interface Agent {
  id: number
  username: string
  display_name: string
  role: string
  status: string
  max_concurrent?: number
  modules?: string[] | null
}

export interface LeadCapture {
  id: number
  customer_id: number
  conversation_id: number
  platform: string
  contact: string
  contact_type: string
  nickname: string
  business: string
  created_at: string
}

export interface Customer {
  id: number
  platform: string
  platform_user_id: string
  nickname: string
  avatar: string
  tags: string[]
  lead_phone: string
  lead_wechat: string
  lead_note: string
  created_at: string
}

export interface Message {
  id: number
  conversation_id: number
  sender_type: 'user' | 'ai' | 'agent' | 'system'
  sender_id?: number | null
  msg_type: string
  content: string
    extra: {
    intent?: string
    confidence?: number
    citations?: Array<string | { title?: string; doc_id?: number | null; chunk_id?: string | null }>
    gaps?: string[]
    dropped?: string[]
    send_failed?: boolean
    send_error?: string
    reflection?: { action?: string; issues?: string[] } | null
    handoff_reason?: string
    reply_ms?: number
    timing?: {
      total_ms?: number
      rewrite_ms?: number
      embed_ms?: number
      rerank_ms?: number
      judge_ms?: number
      draft_ms?: number
      fallback_ms?: number
    }
    filtered_words?: string[]
    media_id?: string
    asr?: boolean
    vision?: boolean
    bypass?: boolean
    event?: string
    to_agent_id?: number
    from_agent_id?: number
    outbox_status?: 'pending' | 'leased' | 'acked' | 'failed' | 'discarded'
    badcase_note?: string
    grounding?: string
    answer_confidence?: {
      faithfulness?: number
      context_precision?: number
      score?: number
      cause?: string
      retried?: boolean
      raw_context_precision?: number | null
    }
    retrieval?: {
      queries?: string[]
      dense_count?: number
      bm25_count?: number
      rerank_top_score?: number | null
      reason?: string
      rerank_status?: string
      gaps?: string[]
      selected?: Array<{ source?: string; score?: number | null; doc_id?: number | null; chunk_id?: string | null }>
    }
  }
  is_internal: boolean
  bad_case: boolean
  created_at: string
}

export interface Conversation {
  id: number
  customer: Customer
  platform: string
  mode: 'ai' | 'human' | 'pending'
  status: string
  assignee_id?: number | null
  assignee_name?: string
  unread_count: number
  summary: string
  last_message_at: string
  created_at: string
  last_message?: Message | null
  wait_seconds?: number
  awaiting_first_response?: boolean
  overdue?: boolean
  transferred_in?: boolean
  is_returning?: boolean
}

export interface KnowledgeChunk {
  id: number
  chunk_index: number
  parent_id?: number | null
  content: string
}

export interface KnowledgeDoc {
  id: number
  title: string
  doc_type: string
  status: string
  version: number
  chunk_count: number
  question?: string
  answer?: string
  category?: string
  similar_questions?: string[]
  hit_count?: number
  created_at: string
  updated_at: string
}

export interface DialogImportResult {
  imported: number
  created: number
  updated: number
  skipped_invalid: number
  skipped_missed: number
  gap_count: number
  gap_titles: string[]
}

export interface DocBatchResult {
  updated: number
  failed_ids: number[]
}

export interface FaqPackResult {
  applied: boolean
  created: number
  content_updated: number
  enabling: number
  disabling_active: number
  skipped_ambiguous: string[]
  duplicates_overridden?: string[]
}

export interface MissedQuestion {
  id: number
  question: string
  platform: string
  count: number
  status: string
  suggested_answer?: string
  conversation_id?: number | null
  created_at: string
}

export interface QuickReply {
  id: number
  title: string
  content: string
  agent_id?: number | null
}

export interface BannedWord {
  id: number
  word: string
  category: string
  direction: string
}

export interface RecallTestResult {
  rewritten_queries: string[]
  hits: { content: string; source: string; score?: number }[]
  rerank_top_score?: number | null
  threshold: number
  passed: boolean
  degraded: boolean
}

export interface StatsOverview {
  today_conversations: number
  today_messages: number
  ai_reply_rate: number
  handoff_rate: number
  avg_first_response_seconds: number
  reply_within_3min_rate: number
  today_tokens: number
  today_token_cost_yuan: number
  active_agents: number
  pending_conversations: number
  total_unread: number
  platform_breakdown: Record<string, number>
  today_comments: number
  today_leads: number
  today_wecom_adds: number
  handoff_reasons_7d?: Record<string, number>
  retrieval_miss_rate_7d?: number
  bad_case_count_7d?: number
  send_failed_count_7d?: number
}

export interface Ticket {
  id: number
  ticket_no: string
  conversation_id: number
  customer_id: number
  type: 'refund' | 'logistics' | 'other'
  title: string
  content: string
  status: 'open' | 'processing' | 'done'
  assignee_id?: number | null
  assignee_name?: string
  created_at: string
  updated_at: string
}

export interface RpaWorker {
  worker_id: string
  account: string
  platform: string
  status: 'online' | 'login_expired' | 'selector_mismatch' | 'offline'
  meta: Record<string, any>
  last_heartbeat_at: string
  pending: number
  failed: number
}

export interface RpaReply {
  outbox_id: number
  account: string
  account_name: string
  profile_id: string
  profile_name: string
  platform: string
  msg_type: string
  kind: 'dm' | 'comment'
  user_account: string
  user_name: string
  question: string
  content: string
  status: string
  error: string
  created_at: string | null
  acked_at: string | null
}

export interface RpaFailedMessage {
  outbox_id: number
  account: string
  platform: string
  conversation_id: string
  content: string
  msg_type: string
  attempts: number
  error: string
  created_at: string
}

export interface AuditLogItem {
  id: number
  agent_name: string
  action: string
  target: string
  detail: string
  created_at: string
}

export interface TokenUsagePoint {
  date: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_yuan: number
}

// ============ 内容矩阵平台 ============

export interface MatrixAccount {
  id: number
  platform: string
  account_name: string
  auth_type: 'api' | 'rpa'
  open_id: string
  rpa_account: string
  status: 'active' | 'expired' | 'disabled'
  has_credentials: boolean
  daily_publish_limit: number
  daily_comment_limit: number
  group_name: string
  queue_enabled: boolean
  queue_slots: string[]
  today_published: number
  profile: { followers?: number; works?: number; liked?: number; updated_at?: string }
  douyin_account_type: string
  created_at: string
}

export interface AccountBinding {
  id: number
  account_id: number
  duty: string
  driver: string
  provider: string
  adspower_profile_id: string
  worker_id: string
  platform_url: string
  auth_status: string
  rpa_account: string
  account_type_mismatch: boolean
  worker_status: string
  debug_file: string
}

export interface AccountBindingInput {
  account_id: number
  duty: string
  driver: string
  provider: string
  adspower_profile_id: string
  worker_id: string
  platform_url?: string
}

export interface AccountBindingOverview {
  douyin_account_type: string
  drivers: { id: string; platform: string; duty: string; label: string; msg_types: string[] }[]
  accounts: {
    id: number
    platform: string
    account_name: string
    group_name: string
    rpa_account: string
    status: string
    confirmed?: boolean
    login_name?: string
    standby_name?: string
    profile_name?: string
    bindings: Record<string, AccountBinding | null>
  }[]
  workers: {
    worker_id: string
    status: string
    account: string
    platform: string
    browser_profiles: { id: string; name: string }[]
    ads_error?: string
  }[]
}

export interface AccountHealth {
  score: number
  level: 'good' | 'warn' | 'bad'
  issues: { code: string; level: string; message: string }[]
  token_expires_at: string | null
  worker_status: string
}

export interface CalendarDay {
  date: string
  tasks: PublishTask[]
}

export interface BestSlots {
  account_id: number
  source: 'history' | 'default'
  slots: string[]
}

export interface ContentVersion {
  id: number
  content_item_id: number
  platform: string
  content_type: 'note' | 'video'
  title: string
  body: string
  tags: string[]
  script: string
  cover_text: string
  material_ids: number[]
  compliance_status: 'pending' | 'passed' | 'failed'
  compliance_report: { hits?: { word: string; reason: string; severity: string }[]; suggestions?: string[] }
  first_comment: string
  dup_report: { max_similarity?: number; similar_version_id?: number; checked_at?: string }
  variant_no: number
  created_at: string
  updated_at: string
}

export interface TitleSuggestion {
  text: string
  formula: string
  score: number
}

export interface AccountReport {
  account_id: number
  account_name: string
  platform: string
  group_name: string
  posts: number
  play: number
  digg: number
  comment: number
  publish_success_rate: number | null
  funnel: Record<string, number>
  health_level: 'good' | 'warn' | 'bad'
  health_score: number
  profile: { followers?: number; works?: number; liked?: number; updated_at?: string }
}

export interface KeywordSuggestion {
  word: string
  heat: string
  covered: boolean
}

export interface ContentItem {
  id: number
  title: string
  topic: string
  selling_points: string[]
  status: 'draft' | 'reviewing' | 'approved' | 'archived'
  created_by?: number | null
  review_note: string
  reviewed_by: number
  reviewed_at: string | null
  inspiration_id: number
  created_at: string
  updated_at: string
  versions?: ContentVersion[]
}

/** 队列运维（Phase 8）：失败任务与队列深度 */
export interface QueueFailedTask {
  id: number
  task_type: string
  error: string
  retries: number
  created_at: string | null
  updated_at: string | null
}

export interface QueueOpsOverview {
  pending: number
  failed: number
  items: QueueFailedTask[]
}

/** 发布弹窗可选版本（审批通过 + 合规通过，后端平铺返回） */
export interface PublishableVersion {
  version_id: number
  item_id: number
  item_title: string
  platform: string
  content_type: string
  variant_no: number
  title: string
  first_comment: string
}

export interface Material {
  id: number
  kind: 'image' | 'video' | 'audio'
  mime: string
  size: number
  duration_seconds: number
  url: string
  created_at: string
}

export interface PublishTask {
  id: number
  content_version_id: number
  account_id: number
  account_name: string
  platform: string
  scheduled_at: string
  status: 'pending' | 'publishing' | 'success' | 'failed' | 'cancelled'
  platform_post_id: string
  post_url: string
  error: string
  retries: number
  published_at?: string | null
  created_at: string
}

export interface Post {
  id: number
  publish_task_id: number
  account_id: number
  account_name: string
  platform: string
  platform_post_id: string
  url: string
  title: string
  stats_json: Record<string, any>
  created_at: string
}

export interface PostComment {
  id: number
  post_id: number
  post_title: string
  platform: string
  platform_comment_id: string
  author_nickname: string
  content: string
  intent: string
  status: 'pending' | 'replied' | 'manual' | 'skipped'
  reply_content: string
  replied_at?: string | null
  created_at: string
}

export interface CommentRule {
  id: number
  platform: string
  intent: string
  keywords: string[]
  reply_templates: string[]
  guide_code: string
  enabled: boolean
  priority: number
  created_at: string
}

export interface FunnelOverview {
  stages: Record<string, number>
  conversion: Record<string, number>
  by_platform: Record<string, Record<string, number>>
  by_account: Record<string, Record<string, number>>
}

export interface AutoSwitches {
  comment_auto_reply_enabled: boolean
  publish_auto_enabled: boolean
}

export interface AnalyticsPost {
  id: number
  account_id: number
  account_name: string
  platform: string
  title: string
  url: string
  stats: Record<string, number>
  stats_updated_at: string | null
  created_at: string
}

export interface PostStatPoint {
  play: number
  digg: number
  comment: number
  share: number
  collect: number
  captured_at: string
}

export interface ContentRoi {
  content_item_id: number
  title: string
  posts: number
  play: number
  digg: number
  comment: number
  funnel: Record<string, number>
}

export interface InspirationItem {
  id: number
  platform: string
  source: 'manual' | 'rpa'
  source_url: string
  author: string
  title: string
  content_text: string
  keyword: string
  stats_json: Record<string, number>
  analysis: {
    title_formula?: string
    structure?: string
    hooks?: string[]
    selling_angle?: string
    why_viral?: string
    reusable_points?: string[]
  }
  status: 'new' | 'analyzed' | 'used'
  created_at: string
}

/** 生产构建的公共前缀，例如 /cs。本地开发 BASE_URL 为 /，这里得到空字符串。 */
export function appBase(): string {
  return (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
}

/** 媒体 URL 构造：<img>/<audio> 标签无法带 Authorization 头，JWT 走 query 参数 */
export function mediaUrl(mediaId: string): string {
  return `${appBase()}/api/media/${mediaId}?token=${encodeURIComponent(getToken() || '')}`
}

const TOKEN_KEY = 'cs_agent_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

/** 下载 FAQ 表。request() 会把成功响应当 JSON 解析，文件要单独走。 */
export async function downloadFaqPack(): Promise<void> {
  const token = getToken()
  const resp = await fetch(`${appBase()}/api/knowledge/faq/export`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (resp.status === 401) {
    setToken(null)
    window.location.href = `${appBase()}/login`
    throw new Error('未登录')
  }
  if (!resp.ok) {
    const text = await resp.text()
    let detail = text
    try {
      detail = JSON.parse(text).detail || text
    } catch { /* 保留原文 */ }
    throw new Error(detail)
  }
  const blob = await resp.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'faq.xlsx'
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
  const resp = await fetch(`${appBase()}${path}`, { ...options, headers: { ...headers, ...(options.headers as any) } })
  // #region agent log
  if (path === '/api/auth/login') {
    fetch('http://127.0.0.1:7468/ingest/3569a161-5930-4d96-8a6f-489725b55d4c',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'aaecc8'},body:JSON.stringify({sessionId:'aaecc8',hypothesisId:'C',location:'client.ts:request',message:'login response',data:{status:resp.status,ok:resp.ok,path},timestamp:Date.now()})}).catch(()=>{});
  }
  // #endregion
  if (resp.status === 401) {
    if (path === '/api/auth/login') {
      const text = await resp.text()
      let detail = '账号或密码错误'
      try {
        detail = JSON.parse(text).detail || detail
      } catch { /* 保留默认文案 */ }
      throw new Error(detail)
    }
    setToken(null)
    window.location.href = `${appBase()}/login`
    throw new Error('未登录')
  }
  if (!resp.ok) {
    const text = await resp.text()
    let detail = text
    try {
      detail = JSON.parse(text).detail || text
    } catch {}
    throw new Error(detail)
  }
  return resp.json()
}

export const api = {
  // auth
  login: (username: string, password: string) =>
    request<{ access_token: string; agent: Agent }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  me: () => request<Agent>('/api/auth/me'),

  // conversations
  listConversations: (tab: string, platform = '') =>
    request<Conversation[]>(
      `/api/conversations?tab=${tab}${platform ? `&platform=${encodeURIComponent(platform)}` : ''}`,
    ),
  conversationCounts: () =>
    request<{ mine: number; active: number; pending: number; closed: number }>('/api/conversations/counts'),
  getConversation: (id: number) => request<Conversation>(`/api/conversations/${id}`),
  listCustomerConversations: (customerId: number) =>
    request<Conversation[]>(`/api/conversations/customer/${customerId}`),
  setMode: (id: number, mode: string) =>
    request(`/api/conversations/${id}/mode`, { method: 'POST', body: JSON.stringify({ mode }) }),
  assign: (id: number, agentId: number, note = '') =>
    request(`/api/conversations/${id}/assign`, {
      method: 'POST',
      body: JSON.stringify({ agent_id: agentId, note }),
    }),
  close: (id: number) => request(`/api/conversations/${id}/close`, { method: 'POST' }),
  updateTags: (id: number, tags: string[]) =>
    request(`/api/conversations/${id}/tags`, { method: 'POST', body: JSON.stringify({ tags }) }),
  reply: (id: number, content: string, isInternal = false, msgType = 'text', mediaId = '') =>
    request<Message>(`/api/conversations/${id}/reply`, {
      method: 'POST',
      body: JSON.stringify({ content, is_internal: isInternal, msg_type: msgType, media_id: mediaId }),
    }),

  // media（工作台图片上传 + 媒体读取）
  uploadMedia: (file: File, kind = 'image') => {
    const form = new FormData()
    form.append('file', file)
    return request<{ media_id: string }>(`/api/media?kind=${kind}`, { method: 'POST', body: form })
  },

  // rpa
  listRpaWorkers: () => request<RpaWorker[]>('/api/rpa/workers'),
  listRpaReplies: (params: {
    kind?: string
    q?: string
    platform?: string
    status?: string
    page?: number
    pageSize?: number
  } = {}) => {
    const search = new URLSearchParams()
    if (params.kind) search.set('kind', params.kind)
    if (params.q) search.set('q', params.q)
    if (params.platform) search.set('platform', params.platform)
    if (params.status) search.set('status', params.status)
    if (params.page) search.set('page', String(params.page))
    if (params.pageSize) search.set('page_size', String(params.pageSize))
    const qs = search.toString()
    return request<{ total: number; items: RpaReply[] }>(`/api/rpa/replies${qs ? `?${qs}` : ''}`)
  },
  listRpaFailed: (account = '') =>
    request<{ items: RpaFailedMessage[] }>(`/api/rpa/outbox/failed?account=${encodeURIComponent(account)}`),
  retryRpaOutbox: (id: number) => request(`/api/rpa/outbox/${id}/retry`, { method: 'POST' }),
  discardRpaOutbox: (id: number) => request(`/api/rpa/outbox/${id}/discard`, { method: 'POST' }),

  // settings（AI 全局熔断开关 + 审计日志）
  getAiSwitch: () => request<{ enabled: boolean }>('/api/settings/ai-switch'),
  getDouyinAccountType: () =>
    request<{ account_type: string }>('/api/settings/douyin-account-type'),
  setDouyinAccountType: (account_type: string) =>
    request<{ account_type: string }>('/api/settings/douyin-account-type', {
      method: 'PUT',
      body: JSON.stringify({ account_type }),
    }),
  setAiSwitch: (enabled: boolean) =>
    request<{ enabled: boolean }>('/api/settings/ai-switch', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  listAuditLogs: (limit = 100) =>
    request<{ items: AuditLogItem[] }>(`/api/settings/audit-logs?limit=${limit}`),

  // evals（badcase 导出为 cases.json 草稿，admin）
  exportBadCases: () => request<any[]>('/api/evals/export'),
  draftEvalCases: () => request<any[]>('/api/evals/drafts', { method: 'POST' }),

  // messages
  listMessages: (conversationId: number) =>
    request<Message[]>(`/api/messages/conversation/${conversationId}`),
  markBadCase: (id: number, badCase: boolean, note = '') =>
    request(`/api/messages/${id}/badcase`, { method: 'POST', body: JSON.stringify({ bad_case: badCase, note }) }),

  // knowledge
  listDocs: () => request<KnowledgeDoc[]>('/api/knowledge/docs'),
  createFaq: (
    title: string,
    question: string,
    answer: string,
    category = '未分类',
    similarQuestions: string[] = [],
  ) =>
    request<KnowledgeDoc>('/api/knowledge/faq', {
      method: 'POST',
      body: JSON.stringify({ title, question, answer, category, similar_questions: similarQuestions }),
    }),
  updateFaq: (
    id: number,
    title: string,
    question: string,
    answer: string,
    category = '未分类',
    similarQuestions: string[] = [],
  ) =>
    request<KnowledgeDoc>(`/api/knowledge/faq/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ title, question, answer, category, similar_questions: similarQuestions }),
    }),
  updateDocMeta: (id: number, patch: { title?: string; category?: string }) =>
    request<KnowledgeDoc>(`/api/knowledge/docs/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  setDocStatus: (id: number, status: 'active' | 'disabled') =>
    request<KnowledgeDoc>(`/api/knowledge/docs/${id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  batchDocs: (ids: number[], action: 'active' | 'disabled' | 'delete') =>
    request<DocBatchResult>('/api/knowledge/docs/batch', {
      method: 'POST',
      body: JSON.stringify({ ids, action }),
    }),
  listChunks: (id: number) => request<KnowledgeChunk[]>(`/api/knowledge/docs/${id}/chunks`),
  deleteDoc: (id: number) => request(`/api/knowledge/docs/${id}`, { method: 'DELETE' }),
  uploadDoc: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<KnowledgeDoc>('/api/knowledge/upload', { method: 'POST', body: form })
  },
  importDialogs: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<DialogImportResult>('/api/knowledge/import-dialogs', { method: 'POST', body: form })
  },
  importFaqPack: (file: File, apply: boolean) => {
    const form = new FormData()
    form.append('file', file)
    form.append('apply', apply ? 'true' : 'false')
    return request<FaqPackResult>('/api/knowledge/faq/import', { method: 'POST', body: form })
  },
  recallTest: (query: string) =>
    request<RecallTestResult>('/api/knowledge/recall-test', {
      method: 'POST',
      body: JSON.stringify({ query }),
    }),
  listMissed: () => request<MissedQuestion[]>('/api/knowledge/missed'),
  resolveMissed: (
    id: number,
    title: string,
    question: string,
    answer: string,
    category = '未分类',
    similarQuestions: string[] = [],
  ) =>
    request<{ ok: boolean; doc_id: number }>(`/api/knowledge/missed/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ title, question, answer, category, similar_questions: similarQuestions }),
    }),
  listBannedWords: () => request<BannedWord[]>('/api/knowledge/banned-words'),
  addBannedWord: (word: string, category = '极限词', direction: 'out' | 'in' = 'out') =>
    request<BannedWord>('/api/knowledge/banned-words', {
      method: 'POST',
      body: JSON.stringify({ word, category, direction }),
    }),
  deleteBannedWord: (id: number) => request(`/api/knowledge/banned-words/${id}`, { method: 'DELETE' }),
  updateBannedWord: (id: number, word: string, category: string, direction: 'out' | 'in') =>
    request<BannedWord>(`/api/knowledge/banned-words/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ word, category, direction }),
    }),

  // quick replies
  listQuickReplies: () => request<QuickReply[]>('/api/quick-replies'),
  createQuickReply: (title: string, content: string, personal = false) =>
    request<QuickReply>('/api/quick-replies', {
      method: 'POST',
      body: JSON.stringify({ title, content, personal }),
    }),
  deleteQuickReply: (id: number) => request(`/api/quick-replies/${id}`, { method: 'DELETE' }),
  updateQuickReply: (id: number, title: string, content: string, personal = false) =>
    request<QuickReply>(`/api/quick-replies/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ title, content, personal }),
    }),

  // agents
  listAgents: () => request<Agent[]>('/api/agents'),
  createAgent: (
    username: string,
    password: string,
    displayName: string,
    role = 'agent',
    maxConcurrent = 20,
    modules?: string[],
  ) =>
    request<Agent>('/api/agents', {
      method: 'POST',
      body: JSON.stringify({
        username,
        password,
        display_name: displayName,
        role,
        max_concurrent: maxConcurrent,
        ...(modules ? { modules } : {}),
      }),
    }),
  updateAgent: (
    id: number,
    data: { max_concurrent?: number; display_name?: string; role?: string; password?: string; modules?: string[] },
  ) => request<Agent>(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteAgent: (id: number) => request(`/api/agents/${id}`, { method: 'DELETE' }),
  setMyStatus: (status: string) =>
    request<Agent>('/api/agents/me/status', { method: 'POST', body: JSON.stringify({ status }) }),

  // stats
  statsOverview: () => request<StatsOverview>('/api/stats/overview'),
  statsTrend: (days = 7) => request<{ date: string; count: number }[]>(`/api/stats/trend?days=${days}`),
  tokenUsage: (days = 7) => request<TokenUsagePoint[]>(`/api/stats/token-usage?days=${days}`),
  missedTop: () => request<{ question: string; count: number }[]>('/api/stats/missed-top'),

  // tickets
  listTickets: (status = '', page = 1) =>
    request<{ total: number; items: Ticket[] }>(`/api/tickets?status=${status}&page=${page}`),
  listLeads: (q = '', platform = '', page = 1) =>
    request<{ total: number; items: LeadCapture[] }>(
      `/api/leads?q=${encodeURIComponent(q)}&platform=${encodeURIComponent(platform)}&page=${page}`,
    ),
  ticketsByConversation: (conversationId: number) =>
    request<Ticket[]>(`/api/tickets/by-conversation/${conversationId}`),
  createTicket: (conversationId: number, type: string, title: string, content: string) =>
    request<Ticket>('/api/tickets', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: conversationId, type, title, content }),
    }),
  updateTicketStatus: (id: number, status: string) =>
    request<Ticket>(`/api/tickets/${id}/status`, { method: 'POST', body: JSON.stringify({ status }) }),
  assignTicket: (id: number, agentId: number) =>
    request<Ticket>(`/api/tickets/${id}/assign`, { method: 'POST', body: JSON.stringify({ agent_id: agentId }) }),

  // mock
  mockIncoming: (platform: string, userId: string, nickname: string, content: string) =>
    request('/api/mock/incoming', {
      method: 'POST',
      body: JSON.stringify({ platform, user_id: userId, nickname, content }),
    }),

  // health
  health: () =>
    request<Record<string, any>>('/api/health'),

  // ============ 内容矩阵平台 ============

  // accounts
  listAccounts: (platform = '') =>
    request<MatrixAccount[]>(`/api/accounts${platform ? `?platform=${platform}` : ''}`),
  createAccount: (data: {
    platform: string; account_name: string; auth_type: string
    rpa_account?: string; daily_publish_limit?: number; daily_comment_limit?: number; group_name?: string
    douyin_account_type?: string
  }) => request<MatrixAccount>('/api/accounts', { method: 'POST', body: JSON.stringify(data) }),
  updateAccount: (id: number, data: Partial<MatrixAccount>) =>
    request<MatrixAccount>(`/api/accounts/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAccount: (id: number) => request(`/api/accounts/${id}`, { method: 'DELETE' }),
  getAccountOauthUrl: (id: number) => request<{ url: string }>(`/api/accounts/${id}/oauth-url`),
  refreshAccountToken: (id: number) =>
    request<MatrixAccount>(`/api/accounts/${id}/refresh`, { method: 'POST' }),
  accountsHealth: () => request<Record<string, AccountHealth>>('/api/accounts/health'),
  accountBindingOverview: () => request<AccountBindingOverview>('/api/account-bindings/overview'),
  createAccountFromProfile: (data: {
    platform: string; duty: string; adspower_profile_id: string
    profile_name?: string; worker_id?: string; douyin_account_type?: string
  }) => request('/api/account-bindings/from-profile', { method: 'POST', body: JSON.stringify(data) }),
  createAccountBinding: (data: AccountBindingInput) =>
    request<AccountBinding>('/api/account-bindings', { method: 'POST', body: JSON.stringify(data) }),
  updateAccountBinding: (id: number, data: Partial<AccountBindingInput> & { auth_status?: string }) =>
    request<AccountBinding>(`/api/account-bindings/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteAccountBinding: (id: number) =>
    request(`/api/account-bindings/${id}`, { method: 'DELETE' }),
  reopenAccountBinding: (id: number) =>
    request(`/api/account-bindings/${id}/reopen`, { method: 'POST' }),
  importAccounts: (lines: string[]) =>
    request<{ imported: number; failed: { line: number; content: string; error: string }[] }>(
      '/api/accounts/import', { method: 'POST', body: JSON.stringify({ lines }) }),

  // contents
  listContents: (status = '') =>
    request<ContentItem[]>(`/api/contents${status ? `?status=${status}` : ''}`),
  listPublishable: () => request<PublishableVersion[]>('/api/contents/publishable'),

  // queue ops（队列运维，仅管理员）
  queueFailed: () => request<QueueOpsOverview>('/api/queue/failed'),
  retryQueueTask: (id: number) =>
    request<{ ok: boolean }>(`/api/queue/${id}/retry`, { method: 'POST' }),
  createContent: (data: { title: string; topic: string; selling_points: string[] }) =>
    request<ContentItem>('/api/contents', { method: 'POST', body: JSON.stringify(data) }),
  getContent: (id: number) => request<ContentItem>(`/api/contents/${id}`),
  generateContent: (id: number, platforms: string[], contentType: string, inspirationId = 0, variants = 1) =>
    request<{ queued: number }>(`/api/contents/${id}/generate`, {
      method: 'POST',
      body: JSON.stringify({ platforms, content_type: contentType, inspiration_id: inspirationId, variants }),
    }),
  updateVersion: (vid: number, data: Partial<ContentVersion>) =>
    request<ContentVersion>(`/api/contents/versions/${vid}`, { method: 'PUT', body: JSON.stringify(data) }),
  checkVersion: (vid: number) =>
    request<{ queued: boolean }>(`/api/contents/versions/${vid}/check`, { method: 'POST' }),
  suggestTitles: (vid: number) =>
    request<{ titles: TitleSuggestion[] }>(`/api/contents/versions/${vid}/titles`, { method: 'POST' }),
  suggestKeywords: (vid: number) =>
    request<{ keywords: KeywordSuggestion[] }>(`/api/contents/versions/${vid}/keywords`, { method: 'POST' }),
  submitContent: (id: number) =>
    request<ContentItem>(`/api/contents/${id}/submit`, { method: 'POST' }),
  reviewContent: (id: number, action: 'approve' | 'reject', note = '') =>
    request<ContentItem>(`/api/contents/${id}/review`, {
      method: 'POST', body: JSON.stringify({ action, note }),
    }),

  // materials
  listMaterials: (kind = '') =>
    request<Material[]>(`/api/materials${kind ? `?kind=${kind}` : ''}`),
  uploadMaterial: (file: File, kind: string) => {
    const form = new FormData()
    form.append('file', file)
    return request<Material>(`/api/materials?kind=${kind}`, { method: 'POST', body: form })
  },

  // publish
  listPublishTasks: (status = '') =>
    request<PublishTask[]>(`/api/publish/tasks${status ? `?status=${status}` : ''}`),
  createPublishTask: (data: { content_version_id: number; account_ids: number[]; scheduled_at?: string | null; force?: boolean }) =>
    request<PublishTask[]>('/api/publish/tasks', { method: 'POST', body: JSON.stringify(data) }),
  cancelPublishTask: (id: number) =>
    request<PublishTask>(`/api/publish/tasks/${id}/cancel`, { method: 'POST' }),
  retryPublishTask: (id: number) =>
    request<PublishTask>(`/api/publish/tasks/${id}/retry`, { method: 'POST' }),
  exportPublishTask: (id: number) => request<Record<string, any>>(`/api/publish/tasks/${id}/export`),
  reschedulePublishTask: (id: number, scheduledAt: string) =>
    request<PublishTask>(`/api/publish/tasks/${id}/reschedule`, {
      method: 'POST', body: JSON.stringify({ scheduled_at: scheduledAt }),
    }),
  publishCalendar: (month = '') =>
    request<CalendarDay[]>(`/api/publish/calendar${month ? `?month=${month}` : ''}`),
  enqueuePublish: (data: { content_version_id: number; account_ids: number[]; force?: boolean }) =>
    request<PublishTask[]>('/api/publish/enqueue', { method: 'POST', body: JSON.stringify(data) }),
  bestSlots: (accountId: number) => request<BestSlots>(`/api/publish/best-slots?account_id=${accountId}`),
  listPosts: () => request<Post[]>('/api/posts'),

  // comments
  listComments: (params: { status?: string; intent?: string; account_id?: number } = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]),
    ).toString()
    return request<PostComment[]>(`/api/comments${qs ? `?${qs}` : ''}`)
  },
  replyComment: (id: number, content: string) =>
    request<PostComment>(`/api/comments/${id}/reply`, { method: 'POST', body: JSON.stringify({ content }) }),
  skipComment: (id: number) => request<PostComment>(`/api/comments/${id}/skip`, { method: 'POST' }),
  listCommentRules: () => request<CommentRule[]>('/api/comments/rules'),
  createCommentRule: (data: Partial<CommentRule>) =>
    request<CommentRule>('/api/comments/rules', { method: 'POST', body: JSON.stringify(data) }),
  updateCommentRule: (id: number, data: Partial<CommentRule>) =>
    request<CommentRule>(`/api/comments/rules/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteCommentRule: (id: number) => request(`/api/comments/rules/${id}`, { method: 'DELETE' }),

  // funnel
  funnelOverview: (days = 7) => request<FunnelOverview>(`/api/funnel/overview?days=${days}`),
  funnelEvents: (stage = '', limit = 100) =>
    request<{ items: any[] }>(`/api/funnel/events?stage=${stage}&limit=${limit}`),

  // auto switches
  getAutoSwitches: () => request<AutoSwitches>('/api/settings/auto-switches'),
  setAutoSwitches: (data: Partial<AutoSwitches>) =>
    request<AutoSwitches>('/api/settings/auto-switches', { method: 'PUT', body: JSON.stringify(data) }),
  getBrandStyle: () => request<{ brand_style_guide: string }>('/api/settings/brand-style'),
  setBrandStyle: (brand_style_guide: string) =>
    request<{ brand_style_guide: string }>('/api/settings/brand-style', {
      method: 'PUT', body: JSON.stringify({ brand_style_guide }),
    }),

  // analytics
  analyticsPosts: (params: { platform?: string; account_id?: number; sort?: string; days?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.platform) q.set('platform', params.platform)
    if (params.account_id) q.set('account_id', String(params.account_id))
    if (params.sort) q.set('sort', params.sort)
    if (params.days) q.set('days', String(params.days))
    return request<AnalyticsPost[]>(`/api/analytics/posts?${q}`)
  },
  postTrend: (postId: number) => request<PostStatPoint[]>(`/api/analytics/posts/${postId}/trend`),
  contentRoi: (days = 30) => request<ContentRoi[]>(`/api/analytics/contents?days=${days}`),
  analyticsAccounts: (days = 30) => request<AccountReport[]>(`/api/analytics/accounts?days=${days}`),

  // inspiration
  listInspiration: (params: { platform?: string; status?: string } = {}) => {
    const q = new URLSearchParams()
    if (params.platform) q.set('platform', params.platform)
    if (params.status) q.set('status', params.status)
    return request<InspirationItem[]>(`/api/inspiration?${q}`)
  },
  createInspiration: (data: { platform: string; source_url?: string; author?: string; title: string; content_text?: string }) =>
    request<InspirationItem>('/api/inspiration', { method: 'POST', body: JSON.stringify(data) }),
  analyzeInspiration: (id: number) =>
    request<InspirationItem>(`/api/inspiration/${id}/analyze`, { method: 'POST' }),
  imitateInspiration: (id: number) =>
    request<{ content_item_id: number; analyzed: boolean }>(`/api/inspiration/${id}/imitate`, { method: 'POST' }),
  deleteInspiration: (id: number) =>
    request<{ ok: boolean }>(`/api/inspiration/${id}`, { method: 'DELETE' }),
}
