/** 全局状态：认证 + WebSocket 事件总线 */
import { create } from 'zustand'
import { Agent, api, appBase, getToken, setToken } from './api/client'

interface AuthState {
  agent: Agent | null
  ready: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  loadMe: () => Promise<void>
  setStatus: (status: string) => Promise<void>
}

export const useAuth = create<AuthState>((set) => ({
  agent: null,
  ready: false,
  login: async (username, password) => {
    const resp = await api.login(username, password)
    setToken(resp.access_token)
    set({ agent: resp.agent })
  },
  logout: () => {
    setToken(null)
    set({ agent: null })
    window.location.href = `${appBase()}/login`
  },
  loadMe: async () => {
    if (!getToken()) {
      set({ ready: true })
      return
    }
    try {
      const agent = await api.me()
      set({ agent, ready: true })
    } catch {
      set({ ready: true })
    }
  },
  setStatus: async (status) => {
    const agent = await api.setMyStatus(status)
    set({ agent })
  },
}))

/** WebSocket 订阅：简单事件总线 */
type WsHandler = (event: string, data: any) => void
const handlers = new Set<WsHandler>()
let ws: WebSocket | null = null

export const WS_RETRY_INITIAL_MS = 1000
export const WS_RETRY_CAP_MS = 30000
let retryDelay = WS_RETRY_INITIAL_MS

/** 指数退避：1s → 2s → 4s … 封顶 30s。onopen 重置为 1s。 */
export function nextWsRetryDelay(current: number): number {
  return Math.min(current * 2, WS_RETRY_CAP_MS)
}

export function subscribeWs(handler: WsHandler): () => void {
  handlers.add(handler)
  ensureWs()
  return () => handlers.delete(handler)
}

function ensureWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
  const token = getToken()
  if (!token) return // 未登录不连接（服务端鉴权，4401 关闭）；登录后组件挂载会再次触发
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${window.location.host}${appBase()}/ws?token=${encodeURIComponent(token)}`)
  ws.onmessage = (e) => {
    try {
      const { event, data } = JSON.parse(e.data)
      handlers.forEach((h) => h(event, data))
    } catch {}
  }
  ws.onclose = () => {
    ws = null
    // 指数退避重连
    setTimeout(ensureWs, retryDelay)
    retryDelay = nextWsRetryDelay(retryDelay)
  }
  ws.onopen = () => {
    retryDelay = WS_RETRY_INITIAL_MS
  }
}

/** 测试用：重置连接状态与退避计数（生产代码勿调用）。 */
export function _resetWsForTests() {
  if (ws) {
    ws.onclose = null
    try { ws.close() } catch { /* ignore */ }
  }
  ws = null
  retryDelay = WS_RETRY_INITIAL_MS
  handlers.clear()
}
