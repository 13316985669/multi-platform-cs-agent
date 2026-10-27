import { Button, Modal, Select, Table, Tabs, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, AccountReport, AnalyticsPost, ContentRoi, PostStatPoint } from '../api/client'
import { toast } from '../components/ui/toast'

const { Title, Text, Link } = Typography

const PLATFORM_LABEL: Record<string, string> = { douyin: '抖音', xiaohongshu: '小红书', mock: 'Mock' }
const FUNNEL_STAGES = [
  { key: 'comment', label: '评论' },
  { key: 'dm', label: '私信' },
  { key: 'lead', label: '留资' },
  { key: 'wecom', label: '加微' },
]
const HEALTH_COLOR: Record<string, string> = { good: 'green', warn: 'gold', bad: 'red' }
const HEALTH_LABEL: Record<string, string> = { good: '健康', warn: '预警', bad: '异常' }

export default function Analytics() {
  const [tab, setTab] = useState<'posts' | 'contents' | 'accounts'>('posts')
  const [sort, setSort] = useState('play')
  const [days, setDays] = useState(30)
  const [posts, setPosts] = useState<AnalyticsPost[]>([])
  const [roi, setRoi] = useState<ContentRoi[]>([])
  const [accountReports, setAccountReports] = useState<AccountReport[]>([])
  const [trend, setTrend] = useState<{ id: number; points: PostStatPoint[] } | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [p, r, a] = await Promise.all([
        api.analyticsPosts({ sort, days }),
        api.contentRoi(days),
        api.analyticsAccounts(days),
      ])
      setPosts(p)
      setRoi(r)
      setAccountReports(a)
    } catch (e: any) {
      toast.error(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [sort, days])

  useEffect(() => {
    load()
  }, [load])

  const openTrend = async (postId: number) => {
    try {
      const points = await api.postTrend(postId)
      setTrend({ id: postId, points })
    } catch (e: any) {
      toast.error(e.message || '趋势加载失败')
    }
  }

  const postColumns: ColumnsType<AnalyticsPost> = [
    {
      title: '作品',
      dataIndex: 'title',
      ellipsis: true,
      render: (_, row) => (
        <div>
          <div>{row.title || '（无标题）'}</div>
          {row.url ? <Link href={row.url} target="_blank">{row.url}</Link> : null}
        </div>
      ),
    },
    {
      title: '平台/账号',
      render: (_, row) => `${PLATFORM_LABEL[row.platform] || row.platform} · ${row.account_name}`,
    },
    { title: '播放', align: 'right', render: (_, row) => (row.stats.play ?? 0).toLocaleString() },
    { title: '点赞', align: 'right', render: (_, row) => (row.stats.digg ?? 0).toLocaleString() },
    { title: '评论', align: 'right', render: (_, row) => (row.stats.comment ?? 0).toLocaleString() },
    { title: '分享', align: 'right', render: (_, row) => (row.stats.share ?? 0).toLocaleString() },
    { title: '收藏', align: 'right', render: (_, row) => (row.stats.collect ?? 0).toLocaleString() },
    {
      title: '更新于',
      align: 'right',
      render: (_, row) => (row.stats_updated_at ? dayjs(row.stats_updated_at).format('MM-DD HH:mm') : '-'),
    },
    {
      title: '',
      width: 80,
      render: (_, row) => <Button type="link" size="small" onClick={() => openTrend(row.id)}>趋势</Button>,
    },
  ]

  const accountColumns: ColumnsType<AccountReport> = [
    {
      title: '账号',
      render: (_, row) => (
        <div>
          <div>{row.account_name}</div>
          <Text type="secondary">{PLATFORM_LABEL[row.platform] || row.platform}</Text>
        </div>
      ),
    },
    { title: '分组', dataIndex: 'group_name', render: (value: string) => value || '-' },
    { title: '粉丝', align: 'right', render: (_, row) => ((row.profile?.followers ?? 0) > 0 ? row.profile!.followers!.toLocaleString() : '-') },
    { title: '发布数', dataIndex: 'posts', align: 'right' },
    { title: '总播放', dataIndex: 'play', align: 'right', render: (value: number) => value.toLocaleString() },
    { title: '总点赞', dataIndex: 'digg', align: 'right', render: (value: number) => value.toLocaleString() },
    { title: '总评论', dataIndex: 'comment', align: 'right', render: (value: number) => value.toLocaleString() },
    { title: '留资', align: 'right', render: (_, row) => row.funnel.lead ?? 0 },
    { title: '加微', align: 'right', render: (_, row) => row.funnel.wecom ?? 0 },
    {
      title: '发布成功率',
      align: 'right',
      render: (_, row) => (row.publish_success_rate === null ? '-' : `${Math.round(row.publish_success_rate * 100)}%`),
    },
    {
      title: '健康',
      align: 'right',
      render: (_, row) => (
        <Tag color={HEALTH_COLOR[row.health_level]}>{HEALTH_LABEL[row.health_level] || row.health_level} {row.health_score}</Tag>
      ),
    },
  ]

  const roiColumns: ColumnsType<ContentRoi> = [
    { title: '选题', dataIndex: 'title', ellipsis: true },
    { title: '发布数', dataIndex: 'posts', align: 'right' },
    { title: '总播放', dataIndex: 'play', align: 'right', render: (value: number) => value.toLocaleString() },
    { title: '总点赞', dataIndex: 'digg', align: 'right', render: (value: number) => value.toLocaleString() },
    ...FUNNEL_STAGES.map((stage) => ({
      title: stage.label,
      align: 'right' as const,
      render: (_: unknown, row: ContentRoi) => row.funnel[stage.key] ?? 0,
    })),
  ]

  return (
    <div className="h-full overflow-auto p-6">
      <Title level={4} style={{ marginBottom: 4 }}>内容效果</Title>
      <Text type="secondary">作品数据自动回采（每 4 小时），选题 ROI = 播放数据 + 引流漏斗，反哺创作选题</Text>
      <div className="mt-3 flex items-center gap-3 flex-wrap">
        <Tabs
          activeKey={tab}
          onChange={(key) => setTab(key as typeof tab)}
          items={[
            { key: 'posts', label: '作品排行' },
            { key: 'contents', label: '选题 ROI' },
            { key: 'accounts', label: '账号对比' },
          ]}
        />
        <span className="flex-1" />
        {tab === 'posts' && (
          <Select
            value={sort}
            onChange={setSort}
            style={{ width: 120 }}
            options={[
              { value: 'play', label: '按播放' },
              { value: 'digg', label: '按点赞' },
              { value: 'comment', label: '按评论' },
              { value: 'share', label: '按分享' },
              { value: 'collect', label: '按收藏' },
            ]}
          />
        )}
        <Select
          value={days}
          onChange={setDays}
          style={{ width: 120 }}
          options={[
            { value: 7, label: '近 7 天' },
            { value: 30, label: '近 30 天' },
            { value: 90, label: '近 90 天' },
          ]}
        />
      </div>
      {tab === 'posts' && (
        <Table rowKey="id" loading={loading} columns={postColumns} dataSource={posts} pagination={false} locale={{ emptyText: '暂无作品数据。发布成功后系统每 4 小时自动回采数据' }} />
      )}
      {tab === 'accounts' && (
        <Table rowKey="account_id" loading={loading} columns={accountColumns} dataSource={accountReports} pagination={false} locale={{ emptyText: '暂无账号数据。添加矩阵账号并发布后，这里展示账号维度对比' }} />
      )}
      {tab === 'contents' && (
        <Table rowKey="content_item_id" loading={loading} columns={roiColumns} dataSource={roi} pagination={false} locale={{ emptyText: '暂无选题数据。内容发布并回采数据后，这里展示每个选题的引流效果' }} />
      )}
      <Modal
        open={!!trend}
        title={trend ? `作品 #${trend.id} 数据趋势` : '数据趋势'}
        footer={null}
        width={640}
        onCancel={() => setTrend(null)}
      >
        {!trend || trend.points.length === 0 ? (
          <div className="text-center py-10">暂无回采数据（下一轮回采后展示）</div>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={trend.points.map((p) => ({
              time: dayjs(p.captured_at).format('MM-DD HH:mm'),
              播放: p.play, 点赞: p.digg, 评论: p.comment,
            }))}>
              <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
              <XAxis dataKey="time" fontSize={11} />
              <YAxis fontSize={11} />
              <Tooltip />
              <Area type="monotone" dataKey="播放" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.15} />
              <Area type="monotone" dataKey="点赞" stroke="#f59e0b" fill="#f59e0b" fillOpacity={0.15} />
              <Area type="monotone" dataKey="评论" stroke="#10b981" fill="#10b981" fillOpacity={0.15} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </Modal>
    </div>
  )
}
