import { Button, Card, Pagination, Select, Space, Tabs, Tag, Typography } from 'antd'
import dayjs from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, Ticket } from '../api/client'
import { toast } from '../components/ui/toast'
import { subscribeWs } from '../store'

const { Title, Text, Paragraph } = Typography

const TYPE_LABEL: Record<string, string> = {
  refund: '退款退货',
  logistics: '物流异常',
  other: '其他',
}

const STATUS_STYLE: Record<string, { label: string; color: string }> = {
  open: { label: '待处理', color: 'orange' },
  processing: { label: '处理中', color: 'blue' },
  done: { label: '已完成', color: 'green' },
}

export default function Tickets() {
  const [tab, setTab] = useState('')
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [agents, setAgents] = useState<{ id: number; display_name: string }[]>([])
  const navigate = useNavigate()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.listTickets(tab, page)
      setTickets(data.items)
      setTotal(data.total)
    } finally {
      setLoading(false)
    }
  }, [tab, page])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    api.listAgents().then(setAgents).catch(() => setAgents([]))
  }, [])

  useEffect(
    () =>
      subscribeWs((event) => {
        if (event === 'ticket_created') load()
      }),
    [load],
  )

  const setStatus = async (ticket: Ticket, status: string) => {
    await api.updateTicketStatus(ticket.id, status)
    toast.success(`工单 ${ticket.ticket_no} 已更新为「${STATUS_STYLE[status]?.label || status}」`)
    load()
  }

  return (
    <div className="h-full overflow-auto p-6">
      <Title level={4} style={{ marginBottom: 4 }}>工单管理</Title>
      <Text type="secondary">AI 自动创建或客服手动创建的售后/物流工单</Text>
      <Tabs
        className="mt-2"
        activeKey={tab}
        onChange={(key) => { setTab(key); setPage(1) }}
        items={[
          { key: '', label: '全部' },
          { key: 'open', label: '待处理' },
          { key: 'processing', label: '处理中' },
          { key: 'done', label: '已完成' },
        ]}
      />
      {loading ? null : tickets.length === 0 ? (
        <Text type="secondary">暂无工单。AI 识别到退款/物流等诉求时会自动建单</Text>
      ) : null}
      <Space direction="vertical" size={12} style={{ width: '100%', maxWidth: 880 }}>
        {tickets.map((ticket) => (
          <Card key={ticket.id} loading={loading} size="small">
            <Space wrap>
              <Text type="secondary" code>{ticket.ticket_no}</Text>
              <Tag>{TYPE_LABEL[ticket.type] || ticket.type}</Tag>
              <Tag color={STATUS_STYLE[ticket.status]?.color}>{STATUS_STYLE[ticket.status]?.label || ticket.status}</Tag>
              <Text type="secondary">{dayjs(ticket.updated_at).format('MM-DD HH:mm')}</Text>
            </Space>
            <div className="mt-2 font-medium">{ticket.title}</div>
            {ticket.assignee_name ? <Text type="secondary">负责人：{ticket.assignee_name}</Text> : null}
            {ticket.content ? <Paragraph type="secondary" style={{ marginBottom: 0 }}>{ticket.content}</Paragraph> : null}
            <Space className="mt-3" wrap>
              {ticket.status === 'open' && (
                <Button size="small" onClick={() => setStatus(ticket, 'processing')}>开始处理</Button>
              )}
              {ticket.status === 'processing' && (
                <Button size="small" type="primary" onClick={() => setStatus(ticket, 'done')}>标记完成</Button>
              )}
              {ticket.status === 'done' && (
                <Button size="small" onClick={() => setStatus(ticket, 'open')}>重新打开</Button>
              )}
              {ticket.status !== 'done' && (
                <Select
                  size="small"
                  placeholder="指派给…"
                  value={ticket.assignee_id || undefined}
                  style={{ width: 140 }}
                  options={agents.map((a) => ({ value: a.id, label: a.display_name }))}
                  onChange={async (id) => {
                    if (!id) return
                    await api.assignTicket(ticket.id, id)
                    toast.success('工单已指派')
                    load()
                  }}
                />
              )}
              <Button
                type="link"
                size="small"
                onClick={() => navigate('/', { state: { conversationId: ticket.conversation_id } })}
              >
                查看会话
              </Button>
            </Space>
          </Card>
        ))}
      </Space>
      {total > 20 && (
        <Pagination
          className="mt-6"
          current={page}
          pageSize={20}
          total={total}
          showSizeChanger={false}
          onChange={setPage}
        />
      )}
    </div>
  )
}
