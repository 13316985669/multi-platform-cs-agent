import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { api, Conversation } from '../api/client'
import ChatWindow from '../components/ChatWindow'
import ConversationList, { InboxTab } from '../components/ConversationList'
import CustomerPanel from '../components/CustomerPanel'
import { subscribeWs, useAuth } from '../store'
import { notifyInbox, stopTitleFlash } from '../utils/notify'

const LIST_WIDTH_KEY = 'cs_agent_list_width'
const MIN_W = 240
const MAX_W = 400

function tabForConversation(conv: Conversation, myId?: number): InboxTab {
  if (conv.status === 'closed') return 'closed'
  if (conv.mode === 'pending') return 'pending'
  if (conv.mode === 'human' && conv.assignee_id && conv.assignee_id === myId) return 'mine'
  return 'active'
}

export default function Workbench() {
  const { agent } = useAuth()
  const [tab, setTab] = useState<InboxTab>('mine')
  const [platform, setPlatform] = useState('')
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [counts, setCounts] = useState({ mine: 0, active: 0, pending: 0, closed: 0 })
  const [currentId, setCurrentId] = useState<number | null>(null)
  const [panelCollapsed, setPanelCollapsed] = useState(false)
  const [draft, setDraft] = useState('')
  const [listWidth, setListWidth] = useState(() => {
    const saved = Number(localStorage.getItem(LIST_WIDTH_KEY))
    return saved >= MIN_W && saved <= MAX_W ? saved : 320
  })
  const dragRef = useRef<{ startX: number; startW: number } | null>(null)
  const location = useLocation()
  const currentIdRef = useRef<number | null>(null)
  currentIdRef.current = currentId

  const refresh = useCallback(async (follow = true) => {
    const [list, c] = await Promise.all([
      api.listConversations(tab, platform),
      api.conversationCounts().catch(() => null),
    ])
    setConversations(list)
    if (c) setCounts({ mine: c.mine ?? 0, active: c.active ?? 0, pending: c.pending ?? 0, closed: c.closed ?? 0 })
    const viewing = currentIdRef.current
    if (follow && viewing != null && !list.some((item) => item.id === viewing)) {
      try {
        const conv = await api.getConversation(viewing)
        if (conv.status === 'open') {
          const next = tabForConversation(conv, agent?.id)
          if (next !== tab) {
            setTab(next)
            window.setTimeout(() => setCurrentId(viewing), 100)
          }
        }
      } catch {
        /* 会话已不存在 */
      }
    }
    return list
  }, [tab, platform, agent?.id])

  useEffect(() => {
    setCurrentId(null)
    refresh(false)
  }, [tab, platform])

  useEffect(() => {
    const targetId = (location.state as { conversationId?: number } | null)?.conversationId
    if (!targetId) return
    api
      .getConversation(targetId)
      .then((conv) => {
        setTab(tabForConversation(conv, agent?.id))
        setTimeout(() => setCurrentId(targetId), 100)
      })
      .catch(() => {})
  }, [location.state, agent?.id])

  useEffect(() => {
    return subscribeWs((event, data) => {
      if (event === 'new_message' || event === 'conversation_update' || event === 'handoff') {
        refresh()
      }
      const convId = data?.conversation_id
      const viewing = convId && convId === currentIdRef.current
      if (event === 'handoff' && !viewing) notifyInbox('pending')
      if (event === 'new_message' && data?.message?.sender_type === 'user' && !viewing) {
        notifyInbox('unread')
      }
    })
  }, [refresh])

  useEffect(() => {
    if (currentId) stopTitleFlash()
  }, [currentId])

  const onDragStart = (e: React.MouseEvent) => {
    dragRef.current = { startX: e.clientX, startW: listWidth }
    const onMove = (ev: MouseEvent) => {
      if (!dragRef.current) return
      const w = Math.min(MAX_W, Math.max(MIN_W, dragRef.current.startW + ev.clientX - dragRef.current.startX))
      setListWidth(w)
    }
    const onUp = () => {
      if (dragRef.current) {
        localStorage.setItem(LIST_WIDTH_KEY, String(listWidthRef.current))
      }
      dragRef.current = null
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }
  const listWidthRef = useRef(listWidth)
  listWidthRef.current = listWidth

  const current = conversations.find((c) => c.id === currentId) || null

  return (
    <div className="h-full flex bg-gray-50 dark:bg-gray-950">
      <ConversationList
        tab={tab}
        onTabChange={setTab}
        conversations={conversations}
        counts={counts}
        currentId={currentId}
        width={listWidth}
        platform={platform}
        onPlatformChange={setPlatform}
        onSelect={(id) => {
          setCurrentId(id)
          setDraft('')
          stopTitleFlash()
          api.getConversation(id).then(() => refresh()).catch(() => {})
        }}
      />
      <div
        onMouseDown={onDragStart}
        className="w-1 cursor-col-resize hover:bg-primary-400/50 active:bg-primary-500/60 shrink-0 transition-colors"
        title="拖拽调整列表宽度"
      />
      {current ? (
        <>
          <ChatWindow
            conversation={current}
            onRefresh={refresh}
            panelCollapsed={panelCollapsed}
            onTogglePanel={() => setPanelCollapsed(!panelCollapsed)}
            draft={draft}
            onDraftChange={setDraft}
          />
          <CustomerPanel
            conversation={current}
            onRefresh={refresh}
            collapsed={panelCollapsed}
            onExpand={() => setPanelCollapsed(false)}
            onInsert={setDraft}
          />
        </>
      ) : (
        <div className="flex-1 flex items-center justify-center text-gray-300 dark:text-gray-600 text-lg">
          选择左侧会话开始接待
        </div>
      )}
    </div>
  )
}
