import { Button, Card, Input, Select, Space, Tabs, Tag } from 'antd'
import { BookOpen, ClipboardList, PanelLeftOpen, Phone, Plus, Tag as TagIcon, UserPlus, X, Zap } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { api, Conversation, Message, QuickReply, RecallTestResult, Ticket } from '../api/client'
import { toast } from './ui/toast'

const TICKET_STATUS: Record<string, { label: string; color: string }> = {
  open: { label: '待处理', color: 'orange' },
  processing: { label: '处理中', color: 'blue' },
  done: { label: '已完成', color: 'green' },
}

const MODE_LABEL: Record<string, string> = {
  ai: 'AI 接待',
  human: '人工接待',
  pending: '排队中',
}

type DockTab = 'customer' | 'quick' | 'knowledge'

const DOCK_TABS: { key: DockTab; label: string }[] = [
  { key: 'customer', label: '客户' },
  { key: 'quick', label: '快捷' },
  { key: 'knowledge', label: '知识' },
]

const TICKET_TYPES = [
  { value: 'refund', label: '退款退货' },
  { value: 'logistics', label: '物流异常' },
  { value: 'other', label: '其他' },
]

interface Props {
  conversation: Conversation
  onRefresh: () => void
  collapsed: boolean
  onExpand: () => void
  onInsert: (text: string) => void
}

function SectionTitle({ children }: { children: ReactNode }) {
  return <div className="text-[12px] text-gray-400 dark:text-gray-500 mb-2">{children}</div>
}

export default function CustomerPanel({ conversation, onRefresh, collapsed, onExpand, onInsert }: Props) {
  const [dockTab, setDockTab] = useState<DockTab>('customer')
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [history, setHistory] = useState<Conversation[]>([])
  const [tagInput, setTagInput] = useState('')
  const [showTicket, setShowTicket] = useState(false)
  const [ticketType, setTicketType] = useState('other')
  const [ticketTitle, setTicketTitle] = useState('')
  const [ticketContent, setTicketContent] = useState('')
  const [quickReplies, setQuickReplies] = useState<QuickReply[]>([])
  const [suggest, setSuggest] = useState<RecallTestResult | null>(null)
  const [suggestLoading, setSuggestLoading] = useState(false)

  const loadSide = () => {
    api.ticketsByConversation(conversation.id).then(setTickets).catch(() => setTickets([]))
    if (conversation.customer?.id) {
      api.listCustomerConversations(conversation.customer.id).then(setHistory).catch(() => setHistory([]))
    }
    api.listQuickReplies().then(setQuickReplies).catch(() => setQuickReplies([]))
  }

  useEffect(() => {
    loadSide()
    setSuggest(null)
  }, [conversation.id])

  useEffect(() => {
    if (conversation.mode !== 'human' || conversation.status !== 'open') {
      setSuggest(null)
      return
    }
    let cancelled = false
    setSuggestLoading(true)
    api
      .listMessages(conversation.id)
      .then((msgs: Message[]) => {
        const lastUser = [...msgs].reverse().find((m) => m.sender_type === 'user' && !m.is_internal)
        if (!lastUser?.content) {
          if (!cancelled) setSuggest(null)
          return null
        }
        return api.recallTest(lastUser.content)
      })
      .then((r) => {
        if (!cancelled && r) setSuggest(r)
      })
      .catch(() => {
        if (!cancelled) setSuggest(null)
      })
      .finally(() => {
        if (!cancelled) setSuggestLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [conversation.id, conversation.mode, conversation.status, conversation.last_message_at])

  if (collapsed) {
    return (
      <div className="w-9 border-l dark:border-gray-700 bg-white dark:bg-gray-900 shrink-0 flex flex-col items-center pt-3">
        <Button
          type="text"
          size="small"
          icon={<PanelLeftOpen size={16} />}
          title="展开右侧栏"
          aria-label="展开右侧栏"
          onClick={onExpand}
        />
      </div>
    )
  }

  const customer = conversation.customer
  const tags = customer?.tags || []
  const hasLead = !!(customer?.lead_phone || customer?.lead_wechat)
  const teamQuick = quickReplies.filter((q) => !q.agent_id)
  const mineQuick = quickReplies.filter((q) => q.agent_id)

  const saveTags = async (next: string[]) => {
    try {
      await api.updateTags(conversation.id, next)
      onRefresh()
    } catch (e: any) {
      toast.error(e?.message || '更新标签失败')
    }
  }

  const addTag = async () => {
    const t = tagInput.trim()
    if (!t || tags.includes(t)) return
    setTagInput('')
    await saveTags([...tags, t])
  }

  const createTicket = async () => {
    if (!ticketTitle.trim()) return
    try {
      await api.createTicket(conversation.id, ticketType, ticketTitle.trim(), ticketContent.trim())
      toast.success('工单已创建')
      setShowTicket(false)
      setTicketTitle('')
      setTicketContent('')
      loadSide()
    } catch (e: any) {
      toast.error(e?.message || '创建工单失败')
    }
  }

  return (
    <div className="w-[280px] border-l dark:border-gray-700 bg-white dark:bg-gray-900 flex flex-col shrink-0">
      <Tabs
        size="small"
        activeKey={dockTab}
        onChange={(key) => setDockTab(key as DockTab)}
        tabBarStyle={{ marginBottom: 0 }}
        className="[&_.ant-tabs-content-holder]:hidden [&_.ant-tabs-nav-list]:w-full [&_.ant-tabs-tab]:flex-1 [&_.ant-tabs-tab]:justify-center"
        items={DOCK_TABS.map((t) => ({ key: t.key, label: t.label }))}
      />

      <div className="flex-1 overflow-y-auto">
        {dockTab === 'customer' && (
          <>
            <div className="p-4 border-b dark:border-gray-700">
              <div className="flex items-center gap-3">
                <div className="w-12 h-12 rounded-full bg-primary-50 text-primary-600 dark:bg-primary-900/40 dark:text-primary-300 flex items-center justify-center text-lg font-medium">
                  {(customer?.nickname || '访')[0]}
                </div>
                <div className="min-w-0">
                  <div className="font-medium text-sm text-gray-800 dark:text-gray-100 truncate">
                    {customer?.nickname || '访客'}
                  </div>
                  <div className="text-xs text-gray-400 dark:text-gray-500">
                    {customer?.platform === 'douyin' ? '抖音' : customer?.platform === 'xiaohongshu' ? '小红书' : '模拟'} 用户
                  </div>
                </div>
              </div>
              <div className="mt-3 text-[11px] text-gray-500 dark:text-gray-400 space-y-0.5">
                <div>接待模式：{MODE_LABEL[conversation.mode] || conversation.mode}</div>
                <div>接待人：{conversation.assignee_name || '未分配'}</div>
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {tags.map((t) => (
                  <Tag
                    key={t}
                    color="cyan"
                    icon={<TagIcon size={9} />}
                    closable
                    closeIcon={<X size={9} />}
                    onClose={(e) => {
                      e.preventDefault()
                      void saveTags(tags.filter((x) => x !== t))
                    }}
                  >
                    {t}
                  </Tag>
                ))}
              </div>
              <Space.Compact className="mt-2" style={{ width: '100%' }}>
                <Input
                  size="small"
                  style={{ flex: 1 }}
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      void addTag()
                    }
                  }}
                  placeholder="添加标签"
                />
                <Button size="small" onClick={() => void addTag()}>
                  添加
                </Button>
              </Space.Compact>
            </div>

            <div className="p-4 border-b dark:border-gray-700">
              <SectionTitle>留资信息</SectionTitle>
              {hasLead ? (
                <Card size="small" className="!bg-green-50 dark:!bg-green-950/40 !border-green-200 dark:!border-green-900">
                  <Space direction="vertical" size={6} className="w-full">
                    {customer.lead_phone && (
                      <div className="flex items-center gap-2 text-sm">
                        <Phone size={13} className="text-green-600 dark:text-green-400" />
                        <span className="font-medium text-green-700 dark:text-green-300">{customer.lead_phone}</span>
                      </div>
                    )}
                    {customer.lead_wechat && (
                      <div className="flex items-center gap-2 text-sm">
                        <UserPlus size={13} className="text-green-600 dark:text-green-400" />
                        <span className="font-medium text-green-700 dark:text-green-300">{customer.lead_wechat}</span>
                      </div>
                    )}
                    {customer.lead_note && (
                      <div className="text-xs text-green-600/80 dark:text-green-400/70 pt-1 border-t border-green-100 dark:border-green-900">
                        {customer.lead_note}
                      </div>
                    )}
                  </Space>
                </Card>
              ) : (
                <div className="text-xs text-gray-300 dark:text-gray-600 bg-gray-50 dark:bg-gray-800 rounded-xl p-3 text-center">
                  暂未留资
                </div>
              )}
              {customer?.created_at && (
                <div className="text-xs text-gray-400 dark:text-gray-500 mt-2">
                  首次咨询：{new Date(customer.created_at).toLocaleDateString('zh-CN')}
                </div>
              )}
            </div>

            <div className="p-4 border-b dark:border-gray-700">
              <div className="flex items-center justify-between mb-2">
                <SectionTitle>
                  <span className="inline-flex items-center gap-1">
                    <ClipboardList size={12} />
                    关联工单
                  </span>
                </SectionTitle>
                <Button
                  type="link"
                  size="small"
                  icon={<Plus size={11} />}
                  className="!-mt-2"
                  onClick={() => setShowTicket(!showTicket)}
                >
                  创建工单
                </Button>
              </div>
              {showTicket && (
                <Space direction="vertical" size={6} className="mb-3 bg-gray-50 dark:bg-gray-800 rounded-xl p-2.5" style={{ width: '100%' }}>
                  <Select
                    size="small"
                    value={ticketType}
                    onChange={setTicketType}
                    options={TICKET_TYPES}
                    style={{ width: '100%' }}
                  />
                  <Input
                    size="small"
                    value={ticketTitle}
                    onChange={(e) => setTicketTitle(e.target.value)}
                    placeholder="工单标题"
                  />
                  <Input.TextArea
                    value={ticketContent}
                    onChange={(e) => setTicketContent(e.target.value)}
                    placeholder="补充说明（可选）"
                    rows={2}
                  />
                  <Button size="small" type="primary" block onClick={() => void createTicket()}>
                    保存工单
                  </Button>
                </Space>
              )}
              {tickets.length === 0 && !showTicket ? (
                <div className="text-xs text-gray-300 dark:text-gray-600 bg-gray-50 dark:bg-gray-800 rounded-xl p-3 text-center">
                  暂无工单
                </div>
              ) : (
                tickets.map((t) => {
                  const st = TICKET_STATUS[t.status] || TICKET_STATUS.open
                  return (
                    <Card key={t.id} size="small" className="!mb-2 !bg-gray-50 dark:!bg-gray-800">
                      <div className="text-sm font-medium text-gray-800 dark:text-gray-100">{t.title}</div>
                      <Space className="mt-1.5" size={8}>
                        <Tag color={st.color}>{st.label}</Tag>
                        <span className="text-xs text-gray-400 dark:text-gray-500">
                          {new Date(t.created_at).toLocaleDateString('zh-CN')}
                        </span>
                      </Space>
                    </Card>
                  )
                })
              )}
            </div>

            <div className="p-4">
              <SectionTitle>历史会话</SectionTitle>
              {history.filter((h) => h.id !== conversation.id).length === 0 ? (
                <div className="text-xs text-gray-300 dark:text-gray-600 text-center py-2">暂无其他会话</div>
              ) : (
                history
                  .filter((h) => h.id !== conversation.id)
                  .map((h) => (
                    <div key={h.id} className="text-xs bg-gray-50 dark:bg-gray-800 rounded-lg px-2.5 py-2 mb-1.5">
                      <div className="flex justify-between text-gray-600 dark:text-gray-300">
                        <span>{MODE_LABEL[h.mode] || h.mode}</span>
                        <span className="text-gray-400">{new Date(h.last_message_at).toLocaleDateString('zh-CN')}</span>
                      </div>
                      <div className="text-gray-400 truncate mt-0.5">{h.last_message?.content || h.summary || '无消息'}</div>
                    </div>
                  ))
              )}
            </div>
          </>
        )}

        {dockTab === 'quick' && (
          <div className="p-3 space-y-4">
            <QuickGroup title="团队" icon={<Zap size={12} />} items={teamQuick} onInsert={onInsert} />
            <QuickGroup title="我的" icon={<Zap size={12} />} items={mineQuick} onInsert={onInsert} />
          </div>
        )}

        {dockTab === 'knowledge' && (
          <div className="p-3">
            <SectionTitle>
              <span className="inline-flex items-center gap-1">
                <BookOpen size={12} />
                召回建议
              </span>
            </SectionTitle>
            {conversation.mode !== 'human' || conversation.status !== 'open' ? (
              <div className="text-xs text-gray-400 bg-gray-50 dark:bg-gray-800 rounded-xl p-3 text-center">
                人工接待时显示知识建议
              </div>
            ) : suggestLoading ? (
              <div className="text-xs text-gray-400 text-center py-6">正在检索…</div>
            ) : !suggest ? (
              <div className="text-xs text-gray-400 text-center py-6">暂无用户消息可检索</div>
            ) : (
              <div className="space-y-1.5">
                {!suggest.passed && (
                  <div className="text-[12px] text-orange-500 px-0.5">无可靠知识，请人工作答</div>
                )}
                {suggest.hits.map((hit, i) => (
                  <Button
                    key={i}
                    type="text"
                    block
                    onClick={() => onInsert(hit.content)}
                    className="!h-auto !justify-start !whitespace-normal !px-2 !py-1.5 !border !border-solid dark:!border-gray-700 hover:!border-primary-400"
                  >
                    <div className="w-full text-left text-xs">
                      <div className="text-[10px] text-gray-400 mb-0.5">
                        {hit.source}
                        {hit.score != null && <span className="ml-2 font-mono">{hit.score.toFixed(2)}</span>}
                      </div>
                      <div className="text-gray-700 dark:text-gray-200 line-clamp-3">{hit.content}</div>
                    </div>
                  </Button>
                ))}
                {!suggest.passed && suggest.hits.length === 0 && (
                  <div className="text-[11px] text-orange-500">未过阈值，不要把不确定的知识发给用户。</div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function QuickGroup({
  title,
  icon,
  items,
  onInsert,
}: {
  title: string
  icon: ReactNode
  items: QuickReply[]
  onInsert: (text: string) => void
}) {
  return (
    <div>
      <div className="text-[12px] text-gray-400 mb-1.5 flex items-center gap-1">
        {icon}
        {title}
      </div>
      {items.length === 0 ? (
        <div className="text-xs text-gray-300 dark:text-gray-600 px-1 py-2">暂无模板</div>
      ) : (
        items.map((qr) => (
          <Button
            key={qr.id}
            type="text"
            block
            onClick={() => onInsert(qr.content)}
            className="!h-auto !justify-start !whitespace-normal !px-2 !py-2 !mb-0.5"
          >
            <div className="w-full text-left">
              <div className="text-[12px] font-medium text-primary-700 dark:text-primary-300">{qr.title}</div>
              <div className="text-[11px] text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">{qr.content}</div>
            </div>
          </Button>
        ))
      )}
    </div>
  )
}
