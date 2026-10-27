import { Input, Select, Space, Table, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import { api, RpaReply } from '../api/client'
import { toast } from '../components/ui/toast'
import { replyBody } from '../utils/accountBinding'

const { Title, Text } = Typography

const PAGE_SIZE = 20

const PLATFORM_LABEL: Record<string, string> = {
  douyin: '抖音',
  xiaohongshu: '小红书',
}

const REPLY_KIND: Record<string, string> = {
  text: '私信',
  image: '私信图片',
  voice: '私信语音',
  comment_reply: '评论',
  first_comment: '首评',
}

const REPLY_STATUS: Record<string, string> = {
  pending: '待发送',
  leased: '发送中',
  acked: '已发出',
  failed: '失败',
}

export function formatReplyTime(value: string | null): string {
  if (!value) return '—'
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value.trim())
  return dayjs(hasZone ? value : `${value}Z`).format('MM-DD HH:mm:ss')
}

export function ReplyContent({ item }: { item: RpaReply }) {
  const body = replyBody(item.msg_type, item.content)
  const error = item.status === 'failed' ? item.error : ''
  const tip = [body, error].filter(Boolean).join('\n')
  const view = (
    <div style={{ maxWidth: 448 }}>
      <div className="truncate">{body || '—'}</div>
      {error ? <div className="truncate text-xs text-red-500">{error}</div> : null}
    </div>
  )
  if (!tip) return view
  return <Tooltip title={<span className="whitespace-pre-wrap">{tip}</span>}>{view}</Tooltip>
}

export default function RpaReplies() {
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('')
  const [platform, setPlatform] = useState('')
  const [status, setStatus] = useState('')
  const [items, setItems] = useState<RpaReply[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.listRpaReplies({
        q: q.trim(),
        kind,
        platform,
        status,
        page,
        pageSize: PAGE_SIZE,
      })
      setItems(data.items)
      setTotal(data.total)
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [q, kind, platform, status, page])

  useEffect(() => {
    load()
  }, [load])

  const columns: ColumnsType<RpaReply> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 150,
      render: (value: string | null) => formatReplyTime(value),
    },
    {
      title: '平台账号',
      dataIndex: 'account_name',
      render: (value: string) => value || '—',
    },
    {
      title: '环境 ID',
      dataIndex: 'profile_id',
      width: 120,
      render: (value: string) => value || '—',
    },
    {
      title: '环境名',
      dataIndex: 'profile_name',
      render: (value: string) => value || '—',
    },
    {
      title: '平台',
      dataIndex: 'platform',
      width: 90,
      render: (value: string) => PLATFORM_LABEL[value] || value || '—',
    },
    {
      title: '类型',
      dataIndex: 'msg_type',
      width: 100,
      render: (value: string) => REPLY_KIND[value] || value,
    },
    {
      title: '用户账号',
      dataIndex: 'user_account',
      render: (value: string) => value || '—',
    },
    {
      title: '用户名称',
      dataIndex: 'user_name',
      width: 120,
      render: (value: string) => value || '—',
    },
    {
      title: '问题',
      dataIndex: 'question',
      render: (value: string) => (
        <div className="truncate" style={{ maxWidth: 280 }}>{value || '—'}</div>
      ),
    },
    {
      title: '回复内容',
      key: 'content',
      render: (_, item) => <ReplyContent item={item} />,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (value: string) => (
        <Text type={value === 'failed' ? 'danger' : undefined}>{REPLY_STATUS[value] || value}</Text>
      ),
    },
  ]

  return (
    <div className="h-full overflow-auto p-6">
      <Title level={4} style={{ marginBottom: 4 }}>RPA 回复记录</Title>
      <Text type="secondary">私信和评论发出后记在这里，可按平台账号、环境、提问人和问题搜索。共 {total} 条</Text>
      <Space className="mt-4 mb-4" wrap>
        <Input
          allowClear
          value={q}
          onChange={(e) => { setPage(1); setQ(e.target.value) }}
          placeholder="搜索平台账号、环境、提问人、问题"
          style={{ width: 360 }}
        />
        <Select
          value={kind}
          onChange={(value) => { setPage(1); setKind(value) }}
          style={{ width: 120 }}
          options={[
            { value: '', label: '全部类型' },
            { value: 'dm', label: '私信' },
            { value: 'comment', label: '评论' },
          ]}
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
        <Select
          value={status}
          onChange={(value) => { setPage(1); setStatus(value) }}
          style={{ width: 140 }}
          options={[
            { value: '', label: '全部状态' },
            { value: 'pending', label: '待发送' },
            { value: 'leased', label: '发送中' },
            { value: 'acked', label: '已发出' },
            { value: 'failed', label: '失败' },
          ]}
        />
      </Space>
      <Table
        rowKey="outbox_id"
        loading={loading}
        columns={columns}
        dataSource={items}
        scroll={{ x: true }}
        locale={{ emptyText: '还没有 RPA 回复。私信和评论发出后会出现在这里' }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          onChange: setPage,
        }}
      />
    </div>
  )
}
