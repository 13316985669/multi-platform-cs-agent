import { Card, Select, Spin, Table, Tag, Typography } from 'antd'
import dayjs from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import { api, FunnelOverview } from '../api/client'
import { toast } from '../components/ui/toast'

const STAGE_META: { key: string; label: string; color: string }[] = [
  { key: 'comment', label: '评论互动', color: '#3b82f6' },
  { key: 'dm', label: '私信暗号', color: '#6366f1' },
  { key: 'lead', label: '留资推送', color: '#f59e0b' },
  { key: 'wecom', label: '加企微', color: '#10b981' },
]

const STAGE_TAG: Record<string, string> = {
  comment: 'blue',
  dm: 'geekblue',
  lead: 'gold',
  wecom: 'green',
}

const CONV_META: { key: string; label: string }[] = [
  { key: 'dm_rate', label: '评论→私信' },
  { key: 'lead_rate', label: '私信→留资' },
  { key: 'wecom_rate', label: '留资→企微' },
]

const PLATFORM_LABEL: Record<string, string> = {
  douyin: '抖音', xiaohongshu: '小红书', mock: 'Mock', unknown: '未识别',
}

export default function Funnel() {
  const [days, setDays] = useState(7)
  const [data, setData] = useState<FunnelOverview | null>(null)
  const [events, setEvents] = useState<any[]>([])
  const [stageFilter, setStageFilter] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [ov, ev] = await Promise.all([
        api.funnelOverview(days),
        api.funnelEvents(stageFilter, 100),
      ])
      setData(ov)
      setEvents(ev.items)
    } catch (e: any) {
      toast.error(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [days, stageFilter])

  useEffect(() => {
    load()
  }, [load])

  const maxStage = Math.max(1, ...(STAGE_META.map((s) => data?.stages[s.key] ?? 0)))

  return (
    <div className="h-full overflow-auto p-6">
      <div className="flex items-center gap-3 mb-4">
        <div className="flex-1">
          <Typography.Title level={4} style={{ marginBottom: 4 }}>引流漏斗</Typography.Title>
          <Typography.Text type="secondary">评论互动 → 私信暗号 → 留资推送 → 加企微，全链路归因到账号与内容</Typography.Text>
        </div>
        <Select
          value={days}
          onChange={setDays}
          style={{ width: 120 }}
          options={[
            { value: 7, label: '近 7 天' },
            { value: 14, label: '近 14 天' },
            { value: 30, label: '近 30 天' },
          ]}
        />
      </div>
      {loading || !data ? <Spin /> : (
        <div className="space-y-4 max-w-5xl">
          <Card>
            <div className="flex items-end justify-between gap-3">
              {STAGE_META.map((s) => {
                const value = data.stages[s.key] ?? 0
                return (
                  <div key={s.key} className="flex-1 text-center">
                    <div className="text-xl font-semibold">{value}</div>
                    <div className="mt-2 mx-auto rounded-t-md" style={{ height: 8 + Math.round((value / maxStage) * 72), background: s.color, maxWidth: 140 }} />
                    <div className="mt-2 text-xs">{s.label}</div>
                  </div>
                )
              })}
            </div>
            <div className="mt-4 pt-3 border-t flex gap-6">
              {CONV_META.map((c) => (
                <div key={c.key} className="text-xs">
                  {c.label}
                  <span className="ml-1.5 font-semibold">{Math.round((data.conversion[c.key] ?? 0) * 100)}%</span>
                </div>
              ))}
            </div>
          </Card>
          <div className="grid grid-cols-2 gap-4">
            {([
              { title: '按平台', map: data.by_platform, labelOf: (k: string) => PLATFORM_LABEL[k] || k },
              { title: '按账号', map: data.by_account, labelOf: (k: string) => k },
            ]).map((block) => (
              <Card key={block.title} title={block.title} size="small">
                <Table
                  size="small"
                  pagination={false}
                  rowKey="name"
                  dataSource={Object.entries(block.map).map(([name, row]) => ({ name: block.labelOf(name), ...row }))}
                  locale={{ emptyText: '暂无数据' }}
                  columns={[
                    { title: '名称', dataIndex: 'name' },
                    ...STAGE_META.map((s) => ({ title: s.label, dataIndex: s.key, align: 'right' as const, render: (value: number) => value ?? 0 })),
                  ]}
                />
              </Card>
            ))}
          </div>
          <Card
            title="事件明细"
            size="small"
            extra={(
              <Select
                value={stageFilter}
                onChange={setStageFilter}
                style={{ width: 140 }}
                options={[{ value: '', label: '全部阶段' }, ...STAGE_META.map((s) => ({ value: s.key, label: s.label }))]}
              />
            )}
          >
            <Table
              size="small"
              rowKey="id"
              pagination={false}
              dataSource={events}
              locale={{ emptyText: '暂无漏斗事件。评论/私信/留资/加粉发生后自动记录' }}
              columns={[
                { title: '时间', dataIndex: 'created_at', width: 120, render: (value: string) => dayjs(value).format('MM-DD HH:mm') },
                {
                  title: '阶段',
                  dataIndex: 'stage',
                  width: 120,
                  render: (value: string) => <Tag color={STAGE_TAG[value]}>{STAGE_META.find((s) => s.key === value)?.label || value}</Tag>,
                },
                { title: '平台', dataIndex: 'platform', render: (value: string) => PLATFORM_LABEL[value] || value || '-' },
                { title: '暗号', dataIndex: 'guide_code', render: (value: string) => value || '—' },
                { title: '客户', dataIndex: 'customer_id', render: (value: number) => (value > 0 ? `#${value}` : '—') },
              ]}
            />
          </Card>
        </div>
      )}
    </div>
  )
}
