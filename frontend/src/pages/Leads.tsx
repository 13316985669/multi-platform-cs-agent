import { Button, Input, Select, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import { api, LeadCapture } from '../api/client'
import LeadConversationModal from '../components/LeadConversationModal'

const { Title, Text } = Typography

const PLATFORM: Record<string, { label: string; color?: string }> = {
  douyin: { label: '抖音' },
  xiaohongshu: { label: '小红书', color: 'red' },
  mock: { label: '模拟' },
}

const TYPE: Record<string, { label: string; color: string }> = {
  phone: { label: '电话', color: 'blue' },
  wechat: { label: '微信', color: 'green' },
}

export default function Leads() {
  const [q, setQ] = useState('')
  const [platform, setPlatform] = useState('')
  const [items, setItems] = useState<LeadCapture[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [viewing, setViewing] = useState<LeadCapture | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.listLeads(q.trim(), platform, page)
      setItems(data.items)
      setTotal(data.total)
    } finally {
      setLoading(false)
    }
  }, [q, platform, page])

  useEffect(() => {
    load()
  }, [load])

  const columns: ColumnsType<LeadCapture> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 120,
      render: (value: string) => dayjs(value).format('MM-DD HH:mm'),
    },
    {
      title: '平台',
      dataIndex: 'platform',
      width: 90,
      render: (value: string) => {
        const badge = PLATFORM[value]
        return badge ? <Tag color={badge.color}>{badge.label}</Tag> : value || '—'
      },
    },
    { title: '昵称', dataIndex: 'nickname', render: (value: string) => value || '—' },
    { title: '联系方式', dataIndex: 'contact' },
    {
      title: '类型',
      dataIndex: 'contact_type',
      width: 90,
      render: (value: string) => {
        const badge = TYPE[value]
        return badge ? <Tag color={badge.color}>{badge.label}</Tag> : value || '—'
      },
    },
    { title: '要办的业务', dataIndex: 'business', render: (value: string) => value || '—' },
    {
      title: '操作',
      width: 110,
      align: 'right',
      render: (_, row) => (
        <Button type="link" size="small" disabled={!row.conversation_id} onClick={() => setViewing(row)}>
          查看会话
        </Button>
      ),
    },
  ]

  return (
    <div className="h-full overflow-auto p-6">
      <Title level={4} style={{ marginBottom: 4 }}>留资</Title>
      <Text type="secondary">顾客留下的电话和微信，可按平台、联系方式、业务搜索。共 {total} 条</Text>
      <Space className="mt-4 mb-4" wrap>
        <Input
          allowClear
          value={q}
          onChange={(e) => { setPage(1); setQ(e.target.value) }}
          placeholder="搜索联系方式、昵称、业务"
          style={{ width: 320 }}
        />
        <Select
          value={platform}
          onChange={(value) => { setPage(1); setPlatform(value) }}
          style={{ width: 140 }}
          options={[
            { value: '', label: '全部平台' },
            { value: 'douyin', label: '抖音' },
            { value: 'xiaohongshu', label: '小红书' },
          ]}
        />
      </Space>
      <Table
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={items}
        locale={{ emptyText: '还没有留资。顾客留下电话或微信后会出现在这里' }}
        pagination={{
          current: page,
          pageSize: 20,
          total,
          showSizeChanger: false,
          onChange: setPage,
        }}
      />
      {viewing && <LeadConversationModal lead={viewing} onClose={() => setViewing(null)} />}
    </div>
  )
}
