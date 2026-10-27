import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setToken } from './api/client'
import {
  nextWsRetryDelay,
  subscribeWs,
  WS_RETRY_CAP_MS,
  WS_RETRY_INITIAL_MS,
  _resetWsForTests,
} from './store'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  url: string
  readyState = FakeWebSocket.CONNECTING
  onopen: ((ev?: Event) => void) | null = null
  onclose: ((ev?: CloseEvent) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({} as CloseEvent)
  }

  open() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.({} as Event)
  }
}

describe('nextWsRetryDelay', () => {
  it('doubles until the 30s cap', () => {
    expect(nextWsRetryDelay(1000)).toBe(2000)
    expect(nextWsRetryDelay(16000)).toBe(WS_RETRY_CAP_MS)
    expect(nextWsRetryDelay(WS_RETRY_CAP_MS)).toBe(WS_RETRY_CAP_MS)
    expect(WS_RETRY_INITIAL_MS).toBe(1000)
  })
})

describe('subscribeWs', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    setToken(null)
    _resetWsForTests()
  })

  afterEach(() => {
    _resetWsForTests()
    setToken(null)
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('does not connect without a token', () => {
    subscribeWs(() => {})
    expect(FakeWebSocket.instances).toHaveLength(0)
  })

  it('attaches JWT as query token', () => {
    setToken('jwt-abc')
    subscribeWs(() => {})
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].url).toContain('/ws?token=jwt-abc')
  })

  it('reconnects with exponential backoff after close', () => {
    setToken('jwt-abc')
    subscribeWs(() => {})
    FakeWebSocket.instances[0].close()
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    FakeWebSocket.instances[1].close()
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(3)
  })
})
