import { Button, Card, Dropdown, Input, Modal, Select, Space } from 'antd'
import clsx from 'clsx'
import {
  AlertCircle,
  ArrowLeftRight,
  Ban,
  BookOpen,
  BookmarkPlus,
  Bot,
  CheckCheck,
  ChevronDown,
  ChevronUp,
  Clock,
  Flag,
  Handshake,
  Image as ImageIcon,
  ImagePlus,
  Mic,
  PanelRightClose,
  PanelRightOpen,
  PhoneOff,
  Smile,
  StickyNote,
  User,
  UserCheck,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Agent, api, Conversation, mediaUrl, Message, QuickReply } from '../api/client'
import { subscribeWs, useAuth } from '../store'
import { promptDialog } from './ui/dialogs'
import { toast } from './ui/toast'

const PLATFORM_LABEL: Record<string, string> = {
  douyin: '抖音',
  xiaohongshu: '小红书',
  mock: '模拟',
}

const MODE_TEXT: Record<string, string> = {
  ai: 'AI 接待中',
  pending: '排队待人工',
  human: '人工接待中',
}

const OUTBOX_STATUS: Record<string, { label: string; cls: string; icon: typeof Clock }> = {
  pending: { label: '已入队', cls: 'text-gray-400', icon: Clock },
  leased: { label: '发送中', cls: 'text-primary-500', icon: Clock },
  acked: { label: '已送达', cls: 'text-green-500', icon: CheckCheck },
  failed: { label: '发送失败', cls: 'text-red-500', icon: AlertCircle },
  discarded: { label: '已忽略', cls: 'text-gray-300 dark:text-gray-600', icon: Ban },
}

const EMOJIS = ['😊', '👍', '🙏', '❤️', '😄', '😢', '😮', '🎉']

function formatTick(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) return hm
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (d.toDateString() === yesterday.toDateString()) return `昨天 ${hm}`
  return `${d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })} ${hm}`
}

const TICK_GAP_MS = 5 * 60 * 1000

function withMessage(prev: Message[], msg: Message): Message[] {
  if (prev.some((m) => m.id === msg.id)) return prev
  return [...prev, msg]
}

interface Props {
  conversation: Conversation
  onRefresh: () => void
  panelCollapsed: boolean
  onTogglePanel: () => void
  draft: string
  onDraftChange: (text: string) => void
}

export default function ChatWindow({
  conversation,
  onRefresh,
  panelCollapsed,
  onTogglePanel,
  draft,
  onDraftChange,
}: Props) {
  const navigate = useNavigate()
  const { agent: me } = useAuth()
  const [messages, setMessages] = useState<Message[]>([])
  const [sending, setSending] = useState(false)
  const [quickReplies, setQuickReplies] = useState<QuickReply[]>([])
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [noteMode, setNoteMode] = useState(false)
  const [aiTyping, setAiTyping] = useState(false)
  const [aiStuck, setAiStuck] = useState(false)
  const [stuckReason, setStuckReason] = useState('')
  const [agents, setAgents] = useState<Agent[]>([])
  const [showTransfer, setShowTransfer] = useState(false)
  const [transferId, setTransferId] = useState(0)
  const [transferNote, setTransferNote] = useState('')
  const [showEmoji, setShowEmoji] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const typingTimer = useRef<ReturnType<typeof setTimeout>>()
  const sendingRef = useRef(false)

  useEffect(() => {
    setSummaryOpen(false)
    setAiTyping(false)
    setAiStuck(false)
    setStuckReason('')
    setNoteMode(false)
    setShowEmoji(false)
    api.listMessages(conversation.id).then(setMessages)
    api.listQuickReplies().then(setQuickReplies)
    api.listAgents().then(setAgents).catch(() => setAgents([]))
  }, [conversation.id])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, aiTyping])

  useEffect(() => {
    const unsubscribe = subscribeWs((event, data) => {
      if (data.conversation_id !== conversation.id) return
      if (event === 'new_message') {
        setMessages((prev) => withMessage(prev, data.message))
        if (data.message.sender_type === 'ai' || data.message.sender_type === 'system') {
          setAiTyping(false)
          setAiStuck(false)
          setStuckReason('')
          clearTimeout(typingTimer.current)
        }
      }
      if (event === 'ai_typing') {
        setAiTyping(true)
        setAiStuck(false)
        setStuckReason('')
        clearTimeout(typingTimer.current)
        typingTimer.current = setTimeout(() => {
          setAiTyping(false)
          setAiStuck(true)
          api.listMessages(conversation.id).then((rows) => {
            setMessages(rows)
            const user = [...rows].reverse().find((m) => m.sender_type === 'user' && !m.is_internal)
            const reason = user?.extra?.retrieval?.reason
            setStuckReason(reason && reason !== 'passed' ? reason : '')
          }).catch(() => setStuckReason(''))
        }, 30000)
      }
    })
    return () => {
      unsubscribe()
      clearTimeout(typingTimer.current)
    }
  }, [conversation.id])

  const send = async (override?: string) => {
    const text = (override ?? draft).trim()
    if (!text || sendingRef.current) return
    sendingRef.current = true
    setSending(true)
    try {
      const msg = await api.reply(conversation.id, text, noteMode)
      if (msg) setMessages((prev) => withMessage(prev, msg))
      onDraftChange('')
    } catch (e: any) {
      toast.error(e?.message || '发送失败，请稍后重试')
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  const sendImage = async (file: File) => {
    try {
      const { media_id } = await api.uploadMedia(file, 'image')
      const msg = await api.reply(conversation.id, '[图片]', false, 'image', media_id)
      if (msg) setMessages((prev) => withMessage(prev, msg))
    } catch (e: any) {
      toast.error(e?.message || '图片发送失败')
    }
  }

  const toggleMode = async () => {
    const newMode = conversation.mode === 'human' ? 'ai' : 'human'
    await api.setMode(conversation.id, newMode)
    onRefresh()
    toast.success(newMode === 'human' ? '已接管，进入人工接待' : '已释放，恢复 AI 接待')
  }

  const claim = async () => {
    if (!me) return
    await api.assign(conversation.id, me.id)
    onRefresh()
    toast.success('已接单')
  }

  const submitTransfer = async () => {
    if (!transferId) return
    await api.assign(conversation.id, transferId, transferNote)
    setShowTransfer(false)
    setTransferNote('')
    onRefresh()
    toast.success('已转接')
  }

  const closeConv = async () => {
    await api.close(conversation.id)
    onRefresh()
    toast.info('会话已结束')
  }

  const markBadCase = async (messageId: number) => {
    const note = await promptDialog({
      title: '标记 badcase',
      placeholder: '备注问题原因（可选），如：答非所问 / 幻觉 / 语气生硬',
      confirmText: '标记',
    })
    if (note === null) return
    await api.markBadCase(messageId, true, note)
    setMessages((prev) => prev.map((m) => (m.id === messageId ? { ...m, bad_case: true } : m)))
    toast.success('已标记为 badcase，可在设置页导出')
  }

  const hashQuery = draft.match(/(?:^|\s)#([^\s#]*)$/)
  const hashHits = hashQuery
    ? quickReplies.filter((q) => {
        const kw = (hashQuery[1] || '').toLowerCase()
        return !kw || q.title.toLowerCase().includes(kw) || q.content.toLowerCase().includes(kw)
      })
    : []

  const otherAgents = agents.filter((a) => a.id !== me?.id)

  return (
    <div className="flex-1 flex flex-col h-full min-w-0 bg-white dark:bg-gray-900">
      <div className="border-b dark:border-gray-700 px-4 py-2 flex items-center gap-3">
        <div className="w-8 h-8 rounded-full bg-primary-500 text-white flex items-center justify-center text-sm font-medium shrink-0">
          {(conversation.customer?.nickname || '访').slice(0, 1)}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm text-gray-800 dark:text-gray-100 truncate">
              {conversation.customer?.nickname || '访客'}
            </span>
            {conversation.status === 'closed' && (
              <span className="text-[10px] text-gray-400">已结束</span>
            )}
          </div>
          <div className="text-[11px] text-gray-400 truncate">
            {PLATFORM_LABEL[conversation.platform] || conversation.platform}
            {conversation.assignee_name ? ` · 接待：${conversation.assignee_name}` : ''}
          </div>
        </div>
        {conversation.status === 'open' && (
          <Space size={0}>
            {conversation.mode === 'pending' && (
              <Button
                type="text"
                size="small"
                icon={<Handshake size={16} />}
                title="接单"
                aria-label="接单"
                className="!text-orange-500"
                onClick={claim}
              />
            )}
            {conversation.mode === 'human' && (
              <Button
                type="text"
                size="small"
                icon={<ArrowLeftRight size={16} />}
                title="转接"
                aria-label="转接"
                onClick={() => {
                  setShowTransfer(true)
                  setTransferId(agents.find((a) => a.id !== me?.id)?.id || 0)
                }}
              />
            )}
            <Button
              type="text"
              size="small"
              icon={conversation.mode === 'human' ? <Bot size={16} /> : <UserCheck size={16} />}
              title={conversation.mode === 'human' ? '释放 AI' : '接管会话'}
              aria-label={conversation.mode === 'human' ? '释放 AI' : '接管会话'}
              onClick={toggleMode}
            />
            <Button
              type="text"
              size="small"
              icon={<PhoneOff size={16} />}
              title="结束会话"
              aria-label="结束会话"
              onClick={closeConv}
            />
          </Space>
        )}
        <Button
          type="text"
          size="small"
          icon={panelCollapsed ? <PanelRightOpen size={16} /> : <PanelRightClose size={16} />}
          title={panelCollapsed ? '展开右侧栏' : '收起右侧栏'}
          aria-label={panelCollapsed ? '展开右侧栏' : '收起右侧栏'}
          onClick={onTogglePanel}
        />
      </div>

      {conversation.status === 'open' && (
        <div className="px-4 py-1 text-[12px] text-gray-500 dark:text-gray-400 border-b dark:border-gray-800 bg-[#fafafa] dark:bg-gray-900">
          {MODE_TEXT[conversation.mode] || conversation.mode}
          {conversation.mode === 'pending' && (
            <span className="text-orange-500 ml-2">用户已申请人工，请尽快接管</span>
          )}
        </div>
      )}

      <Modal
        open={showTransfer}
        title="转接给同事"
        okText="确认转接"
        cancelText="取消"
        okButtonProps={{ disabled: !transferId }}
        onOk={submitTransfer}
        onCancel={() => setShowTransfer(false)}
      >
        <Space direction="vertical" size={8} className="w-full">
          <Select
            value={transferId || undefined}
            placeholder="选择同事"
            style={{ width: '100%' }}
            options={otherAgents.map((a) => ({
              value: a.id,
              label: `${a.display_name}（${a.status === 'active' ? '接待中' : a.status === 'resting' ? '休息中' : '离线'}）`,
            }))}
            onChange={(value) => setTransferId(value)}
          />
          <Input
            value={transferNote}
            onChange={(e) => setTransferNote(e.target.value)}
            placeholder="交接备注（内部可见，可选）"
          />
        </Space>
      </Modal>

      {conversation.summary && (
        <div className="bg-amber-50/70 dark:bg-amber-950/30 border-b border-amber-100 dark:border-amber-900/50 px-4 py-1.5">
          <Button
            type="text"
            size="small"
            icon={summaryOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            onClick={() => setSummaryOpen(!summaryOpen)}
            className="!text-amber-700 dark:!text-amber-400 !font-medium"
          >
            会话小结
          </Button>
          {summaryOpen && (
            <p className="text-xs text-amber-600 dark:text-amber-400/80 mt-1 leading-relaxed whitespace-pre-wrap">
              {conversation.summary}
            </p>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4 space-y-3 bg-[#f4f5f7] dark:bg-gray-900">
        {messages.map((msg, i) => {
          const prev = messages[i - 1]
          const showTick =
            !prev || new Date(msg.created_at).getTime() - new Date(prev.created_at).getTime() > TICK_GAP_MS
          const priorUser = [...messages.slice(0, i)].reverse().find((m) => m.sender_type === 'user' && !m.is_internal)
          const canCapture = (msg.sender_type === 'user' || msg.sender_type === 'ai') && !msg.is_internal
          return (
            <div key={msg.id}>
              {showTick && (
                <div className="text-center text-[10px] text-gray-400 dark:text-gray-500 py-1 select-none">
                  {formatTick(msg.created_at)}
                </div>
              )}
              <MessageBubble
                msg={msg}
                priorUser={priorUser}
                onMarkBadCase={markBadCase}
                onCapture={
                  canCapture
                    ? () => {
                        const question = msg.sender_type === 'user' ? msg.content : priorUser?.content || ''
                        const answer = msg.sender_type === 'ai' ? msg.content : ''
                        navigate('/knowledge', {
                          state: {
                            faqDraft: {
                              title: question.slice(0, 30),
                              question,
                              answer,
                            },
                          },
                        })
                      }
                    : undefined
                }
              />
            </div>
          )
        })}
        {aiTyping && <TypingBubble />}
        {aiStuck && !aiTyping && (
          <div className="text-center text-xs text-amber-600 dark:text-amber-400 py-1">
            AI 未响应{stuckReason ? `（${stuckReason}）` : ''}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {conversation.status === 'open' && (
        <div className={clsx('border-t dark:border-gray-700 px-3 pt-1.5 pb-2.5 bg-white dark:bg-gray-900', noteMode && 'bg-amber-50/60 dark:bg-amber-950/20')}>
          <Space size={0} className="mb-1">
            <Dropdown
              open={showEmoji}
              onOpenChange={setShowEmoji}
              trigger={['click']}
              dropdownRender={() => (
                <Card size="small" styles={{ body: { padding: 6 } }}>
                  <Space size={4}>
                    {EMOJIS.map((em) => (
                      <Button
                        key={em}
                        type="text"
                        size="small"
                        onClick={() => {
                          onDraftChange(draft + em)
                          setShowEmoji(false)
                        }}
                      >
                        {em}
                      </Button>
                    ))}
                  </Space>
                </Card>
              )}
            >
              <Button
                type="text"
                size="small"
                icon={<Smile size={16} />}
                title="表情 / 快捷回复见右侧"
                aria-label="表情"
                className={showEmoji ? '!bg-primary-50 !text-primary-600' : undefined}
              />
            </Dropdown>
            <Button
              type="text"
              size="small"
              icon={<ImagePlus size={16} />}
              title="发送图片"
              aria-label="发送图片"
              onClick={() => fileRef.current?.click()}
            />
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) sendImage(f)
                e.target.value = ''
              }}
            />
            <Button
              type="text"
              size="small"
              icon={<StickyNote size={16} />}
              title="内部备注（仅客服可见，不发给用户）"
              aria-label="内部备注"
              onClick={() => setNoteMode(!noteMode)}
              className={noteMode ? '!bg-amber-100 !text-amber-700 dark:!bg-amber-950 dark:!text-amber-400' : undefined}
            >
              {noteMode ? <span className="text-[10px] font-medium">备注</span> : null}
            </Button>
          </Space>

          <div className="relative">
            {hashHits.length > 0 && (
              <Card
                size="small"
                className="absolute bottom-full left-0 right-0 mb-1 z-10 shadow-pop"
                styles={{ body: { padding: 4, maxHeight: 160, overflowY: 'auto' } }}
              >
                {hashHits.slice(0, 8).map((qr) => (
                  <Button
                    key={qr.id}
                    type="text"
                    block
                    onClick={() => onDraftChange(qr.content)}
                    className="!h-auto !justify-start !whitespace-normal !px-3 !py-1.5"
                  >
                    <div className="w-full text-left text-xs">
                      <span className="text-primary-600 font-medium">#{qr.title}</span>
                      <span className="text-gray-400 ml-2">{qr.agent_id ? '我的' : '团队'}</span>
                      <div className="text-gray-500 truncate">{qr.content}</div>
                    </div>
                  </Button>
                ))}
              </Card>
            )}
            <Input.TextArea
              value={draft}
              onChange={(e) => onDraftChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  if (e.nativeEvent.isComposing || e.keyCode === 229) return
                  e.preventDefault()
                  // 只输入了 #、后面没有关键词时，列表是全部模板，Enter 仍应发送
                  if (hashHits.length > 0 && hashQuery?.[1]) {
                    onDraftChange(hashHits[0].content)
                    return
                  }
                  send()
                }
              }}
              placeholder={
                noteMode
                  ? '内部备注：仅客服可见，不会发给用户…'
                  : conversation.mode === 'ai'
                    ? 'AI 接待中，人工可直接插话…（Enter 发送）'
                    : '输入回复内容…（Enter 发送）'
              }
              rows={2}
              style={{ paddingRight: 64, resize: 'none' }}
              className={noteMode ? '!border-amber-300 !bg-amber-50 dark:!border-amber-800 dark:!bg-amber-950/40' : undefined}
            />
            <Button
              type="primary"
              size="small"
              onClick={() => send()}
              disabled={sending || !draft.trim()}
              className="!absolute right-2 bottom-2"
              style={noteMode ? { background: '#f59e0b', borderColor: '#f59e0b' } : undefined}
            >
              发送
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

function TypingBubble() {
  return (
    <div className="flex justify-end bubble-in">
      <div className="flex items-end gap-2 flex-row-reverse">
        <div className="w-7 h-7 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center shrink-0">
          <Bot size={14} />
        </div>
        <div className="bg-primary-50 dark:bg-primary-700 rounded-2xl rounded-tr-sm px-4 py-3 flex gap-1">
          {[0, 1, 2].map((i) => (
            <span key={i} className="typing-dot w-1.5 h-1.5 rounded-full bg-primary-500 dark:bg-white" />
          ))}
        </div>
      </div>
    </div>
  )
}

function formatAnswerDuration(totalMs: number): string {
  if (totalMs >= 1000) return `${(totalMs / 1000).toFixed(1)}秒`
  return `${Math.round(totalMs)}毫秒`
}

type CitationItem = string | { title?: string; doc_id?: number | null; chunk_id?: string | null }

function citationParts(item: CitationItem): { title: string; docId: number | null } {
  if (typeof item === 'string') return { title: item, docId: null }
  return { title: item.title || '资料', docId: item.doc_id ?? null }
}

function CitationBlock({
  citations,
  intent,
  confidence,
}: {
  citations: CitationItem[]
  intent?: string
  confidence?: number
}) {
  const [open, setOpen] = useState(false)
  const meta = [intent, confidence != null ? `${Math.round(confidence * 100)}%` : '']
    .filter(Boolean)
    .join(' · ')

  return (
    <div className="min-w-0 max-w-full">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="inline-flex max-w-full cursor-pointer items-center gap-1 rounded-full border border-solid border-primary-200 bg-white/90 px-2 py-0.5 text-[11px] leading-none text-primary-700 hover:bg-primary-50 dark:border-primary-700 dark:bg-gray-800 dark:text-primary-200 dark:hover:bg-gray-700"
      >
        <BookOpen size={11} className="shrink-0" />
        <span className="shrink-0">引用 {citations.length} 条</span>
        {meta && <span className="truncate text-gray-400 dark:text-gray-500">{meta}</span>}
        {open ? <ChevronUp size={11} className="shrink-0" /> : <ChevronDown size={11} className="shrink-0" />}
      </button>
      {open && (
        <ul className="mt-1 min-w-[220px] max-w-full rounded-xl border border-gray-200 bg-white px-2.5 py-1 shadow-card dark:border-gray-700 dark:bg-gray-800">
          {citations.map((item, i) => {
            const { title, docId } = citationParts(item)
            return (
              <li key={i} className="flex items-center gap-2 py-1 text-[11px] text-gray-700 dark:text-gray-200">
                <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-primary-50 text-[10px] text-primary-700 dark:bg-primary-700/40 dark:text-primary-100">
                  {i + 1}
                </span>
                <span className="min-w-0 flex-1 truncate">{title}</span>
                {docId != null && <span className="shrink-0 text-gray-400">#{docId}</span>}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

function MessageBubble({
  msg,
  priorUser,
  onMarkBadCase,
  onCapture,
}: {
  msg: Message
  priorUser?: Message
  onMarkBadCase: (id: number) => void
  onCapture?: () => void
}) {
  const isUser = msg.sender_type === 'user'

  if (msg.sender_type === 'system') {
    return <div className="text-center text-xs text-gray-400 dark:text-gray-500 py-1">{msg.content}</div>
  }

  if (msg.is_internal) {
    return (
      <div className="flex justify-end bubble-in">
        <div className="max-w-[70%] border border-dashed border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/40 rounded-xl px-3.5 py-2">
          <div className="flex items-center gap-1 text-[10px] text-amber-600 dark:text-amber-400 font-medium mb-0.5">
            <StickyNote size={10} />
            内部备注 · 仅客服可见
          </div>
          <div className="text-sm text-amber-800 dark:text-amber-300 leading-relaxed break-words">
            {msg.content}
          </div>
        </div>
      </div>
    )
  }

  const mediaSrc = msg.extra?.media_id ? mediaUrl(msg.extra.media_id) : null
  const outbox = msg.extra?.outbox_status ? OUTBOX_STATUS[msg.extra.outbox_status] : null
  const fullTime = new Date(msg.created_at).toLocaleString('zh-CN')
  const isAi = msg.sender_type === 'ai'
  const answerMs = msg.extra?.timing?.total_ms

  const avatar = isUser ? (
    <div className="w-7 h-7 rounded-full bg-gray-200 dark:bg-gray-700 text-gray-500 dark:text-gray-300 flex items-center justify-center shrink-0">
      <User size={14} />
    </div>
  ) : isAi ? (
    <div className="w-7 h-7 rounded-full bg-primary-100 text-primary-700 flex items-center justify-center shrink-0">
      <Bot size={14} />
    </div>
  ) : (
    <div className="w-7 h-7 rounded-full bg-primary-600 flex items-center justify-center text-white shrink-0">
      <UserCheck size={14} />
    </div>
  )

  return (
    <div className={clsx('flex bubble-in', isUser ? 'justify-start' : 'justify-end')}>
      <div className={clsx('flex items-start gap-2 max-w-[75%]', !isUser && 'flex-row-reverse')}>
        {avatar}
        <div className="min-w-0">
          <div className={clsx('mb-0.5 flex items-center gap-1', !isUser && 'justify-end')}>
            {isAi && (
              <span className="text-[10px] text-gray-400">机器人</span>
            )}
            {msg.extra?.filtered_words && msg.extra.filtered_words.length > 0 && (
              <span className="text-[10px] text-orange-500">已过滤敏感词</span>
            )}
          </div>

          <div
            title={fullTime}
            className={clsx(
              'rounded-2xl px-3.5 py-2 text-sm leading-relaxed break-words whitespace-pre-wrap shadow-sm',
              isUser
                ? 'bg-white dark:bg-gray-800 dark:text-gray-50 border border-gray-200 dark:border-gray-600 rounded-tl-sm'
                : isAi
                  ? 'bg-primary-100 text-primary-800 dark:bg-primary-700 dark:text-white rounded-tr-sm'
                  : 'bg-primary-600 text-white rounded-tr-sm',
            )}
          >
            {msg.msg_type === 'image' && mediaSrc ? (
              <a href={mediaSrc} target="_blank" rel="noreferrer">
                <img src={mediaSrc} alt="图片消息" className="max-w-[220px] rounded-lg bg-white/10" loading="lazy" />
              </a>
            ) : msg.msg_type === 'voice' && mediaSrc ? (
              <audio controls src={mediaSrc} className="max-w-[220px] h-8" />
            ) : (
              msg.content
            )}
            {msg.msg_type === 'voice' && (
              <div className={clsx('text-[10px] mt-1 flex items-center gap-1', isUser ? 'text-gray-400' : isAi ? 'text-primary-600 dark:text-white/70' : 'text-white/70')}>
                <Mic size={10} />
                {msg.extra?.asr ? '语音转文字' : '语音消息'}
              </div>
            )}
            {msg.msg_type === 'image' && !mediaSrc && (
              <div className="flex items-center gap-1 text-xs opacity-80">
                <ImageIcon size={12} />
                {msg.content}
              </div>
            )}
          </div>

          <div className={clsx('mt-0.5 flex items-center gap-2 text-[10px] text-gray-400 dark:text-gray-500', !isUser && 'justify-end')}>
            {isAi && answerMs != null && (
              <span title="从开始生成到这条回复入库">{formatAnswerDuration(answerMs)}</span>
            )}
            {outbox && (
              <span className={clsx('inline-flex items-center gap-0.5', outbox.cls)} title={`RPA 通道：${outbox.label}`}>
                <outbox.icon size={10} />
                {outbox.label}
              </span>
            )}
            {msg.extra?.send_failed && (
              <span className="text-red-500">未送达</span>
            )}
            {isAi && (priorUser?.extra?.retrieval || msg.extra?.dropped?.length || msg.extra?.gaps?.length || msg.extra?.handoff_reason) && (
              <details className="text-left">
                <summary className="cursor-pointer">检索</summary>
                <div className="mt-1 max-w-xs text-gray-500">
                  {priorUser?.extra?.retrieval?.reason && <div>原因 {priorUser.extra.retrieval.reason}</div>}
                  {priorUser?.extra?.retrieval?.rerank_top_score != null && (
                    <div>精排 {priorUser.extra.retrieval.rerank_top_score}</div>
                  )}
                  {msg.extra?.answer_confidence && (
                    <div>
                      忠实度 {msg.extra.answer_confidence.faithfulness ?? '—'} · 上下文精度{' '}
                      {msg.extra.answer_confidence.context_precision ?? '—'} · 综合{' '}
                      {msg.extra.answer_confidence.score ?? '—'}
                    </div>
                  )}
                  {!!priorUser?.extra?.retrieval?.selected?.length && (
                    <div>
                      入选{' '}
                      {priorUser.extra.retrieval.selected
                        .map((item) => `${item.source || '资料'}${item.score != null ? ` ${item.score}` : ''}`)
                        .join('、')}
                    </div>
                  )}
                  {!!msg.extra?.gaps?.length && <div>缺口 {msg.extra.gaps.join('、')}</div>}
                  {!!msg.extra?.dropped?.length && <div>删掉 {msg.extra.dropped.join('、')}</div>}
                  {!!msg.extra?.handoff_reason && <div>转人工 {msg.extra.handoff_reason}</div>}
                </div>
              </details>
            )}
            {msg.bad_case && (
              <span className="inline-flex items-center gap-0.5 text-red-400" title={msg.extra?.badcase_note || ''}>
                <Flag size={10} />
                已标记
              </span>
            )}
          </div>

          {(!!msg.extra?.citations?.length || (isAi && !msg.bad_case) || onCapture) && (
            <div className={clsx('mt-1.5 flex items-start gap-1.5', !isUser && 'justify-end')}>
              {!!msg.extra?.citations?.length && (
                <CitationBlock citations={msg.extra.citations} intent={msg.extra.intent} confidence={msg.extra.confidence} />
              )}
              {isAi && !msg.bad_case && (
                <button
                  type="button"
                  onClick={() => onMarkBadCase(msg.id)}
                  className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-full border border-solid border-gray-200 bg-white/90 px-2 py-0.5 text-[11px] leading-none text-gray-500 hover:border-red-200 hover:bg-red-50 hover:text-red-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-red-800 dark:hover:bg-red-950/40 dark:hover:text-red-400"
                  title="标记为 badcase"
                >
                  <Flag size={11} />
                  不佳
                </button>
              )}
              {onCapture && (
                <button
                  type="button"
                  onClick={onCapture}
                  className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-full border border-solid border-gray-200 bg-white/90 px-2 py-0.5 text-[11px] leading-none text-gray-500 hover:border-primary-200 hover:bg-primary-50 hover:text-primary-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:border-primary-700 dark:hover:bg-gray-700 dark:hover:text-primary-200"
                  title="带进知识库编辑，不会直接保存"
                >
                  <BookmarkPlus size={11} />
                  沉淀
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
