import { Button, DatePicker, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import clsx from 'clsx'
import dayjs from 'dayjs'
import { CalendarClock, CalendarDays, ChevronLeft, ChevronRight, Download, List, Plus, RotateCcw, XCircle, Zap } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, CalendarDay, MatrixAccount, PublishableVersion, PublishTask } from '../api/client'
import Empty from '../components/ui/Empty'
import { confirmDialog } from '../components/ui/dialogs'
import { toast } from '../components/ui/toast'
import { subscribeWs } from '../store'
import { PLATFORM_LABEL, publishableLabel } from '../utils/labels'

const { Title, Text, Link } = Typography

const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  pending: { label: '待发布', cls: 'bg-blue-100 text-blue-600 dark:bg-blue-900/40 dark:text-blue-400' },
  publishing: { label: '发布中', cls: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/40 dark:text-yellow-400' },
  success: { label: '已发布', cls: 'bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-400' },
  failed: { label: '失败', cls: 'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-400' },
  cancelled: { label: '已取消', cls: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400' },
}

const STATUS_COLOR: Record<string, string> = {
  pending: 'blue',
  publishing: 'gold',
  success: 'green',
  failed: 'red',
  cancelled: 'default',
}

// 平台标签统一从 utils/labels 引入

export default function PublishCalendar() {
  const navigate = useNavigate()
  const [tasks, setTasks] = useState<PublishTask[]>([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [publishable, setPublishable] = useState<PublishableVersion[]>([])
  const [accounts, setAccounts] = useState<MatrixAccount[]>([])
  const [form, setForm] = useState({ version_id: 0, account_ids: [] as number[], scheduled_at: '' })
  const [view, setView] = useState<'list' | 'calendar'>('list')
  const [month, setMonth] = useState(dayjs().format('YYYY-MM'))
  const [calDays, setCalDays] = useState<CalendarDay[]>([])
  const [calLoading, setCalLoading] = useState(false)
  const [bestSlotHint, setBestSlotHint] = useState('')

  const load = useCallback(async () => {
    try {
      setTasks(await api.listPublishTasks())
    } catch (e: any) {
      toast.error(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadCalendar = useCallback(async () => {
    setCalLoading(true)
    try {
      setCalDays(await api.publishCalendar(month))
    } catch (e: any) {
      toast.error(e.message || '日历加载失败')
    } finally {
      setCalLoading(false)
    }
  }, [month])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (view === 'calendar') loadCalendar()
  }, [view, loadCalendar])

  useEffect(
    () =>
      subscribeWs((event) => {
        if (event === 'publish_task_updated') {
          load()
          if (view === 'calendar') loadCalendar()
        }
      }),
    [load, loadCalendar, view],
  )

  // 选中单个账号时拉取最佳时段提示
  useEffect(() => {
    if (form.account_ids.length !== 1) {
      setBestSlotHint('')
      return
    }
    api.bestSlots(form.account_ids[0])
      .then((r) => setBestSlotHint(
        `推荐时段：${r.slots.join(' / ')}（${r.source === 'history' ? '按该账号历史播放数据' : '平台黄金时段默认'}）`))
      .catch(() => setBestSlotHint(''))
  }, [form.account_ids])

  const enqueue = async (force = false) => {
    if (!form.version_id || form.account_ids.length === 0) {
      toast.error('请选择内容版本和账号')
      return
    }
    try {
      const created = await api.enqueuePublish({
        content_version_id: form.version_id, account_ids: form.account_ids, force,
      })
      toast.success(`已加入队列：${created.map((t) => `${t.account_name} ${dayjs(t.scheduled_at).format('MM-DD HH:mm')}`).join('；')}`)
      setShowCreate(false)
      load()
      if (view === 'calendar') loadCalendar()
    } catch (e: any) {
      if (!force && typeof e.message === 'string' && e.message.includes('force=true')) {
        const ok = await confirmDialog({
          title: '检测到高相似内容',
          message: e.message.replace('，确认仍要发布请带 force=true', '。确认仍要入队？'),
          confirmText: '仍要入队',
          danger: true,
        })
        if (ok) return enqueue(true)
        return
      }
      toast.error(e.message || '入队失败')
    }
  }

  const dropToDay = async (date: string) => {
    const taskId = Number(sessionStorage.getItem('drag_task_id') || 0)
    if (!taskId) return
    const task = tasks.find((t) => t.id === taskId)
      || calDays.flatMap((d) => d.tasks).find((t) => t.id === taskId)
    if (!task || task.status !== 'pending') return
    const newTime = dayjs(task.scheduled_at)
    const target = dayjs(date).hour(newTime.hour()).minute(newTime.minute())
    if (target.isBefore(dayjs())) {
      toast.error('不能改到过去的时间')
      return
    }
    try {
      await api.reschedulePublishTask(taskId, target.toISOString())
      toast.success(`已改期到 ${target.format('MM-DD HH:mm')}`)
      load()
      loadCalendar()
    } catch (e: any) {
      toast.error(e.message || '改期失败')
    }
  }

  const openCreate = async () => {
    try {
      // 一次平铺查询拿到「审批+合规双通过」的版本，替代逐条拉详情的 N+1
      const [versions, accs] = await Promise.all([api.listPublishable(), api.listAccounts()])
      setPublishable(versions)
      setAccounts(accs.filter((a) => a.status === 'active'))
      setForm({ version_id: 0, account_ids: [], scheduled_at: '' })
      setShowCreate(true)
    } catch (e: any) {
      toast.error(e.message || '加载失败')
    }
  }

  const create = async (force = false) => {
    if (!form.version_id || form.account_ids.length === 0) {
      toast.error('请选择内容版本和账号')
      return
    }
    try {
      await api.createPublishTask({
        content_version_id: form.version_id,
        account_ids: form.account_ids,
        scheduled_at: form.scheduled_at ? new Date(form.scheduled_at).toISOString() : null,
        force,
      })
      toast.success('发布任务已创建（自动发布开关开启后按时间执行）')
      setShowCreate(false)
      load()
    } catch (e: any) {
      // 查重软拦截（409）：弹确认后带 force 重试
      if (!force && typeof e.message === 'string' && e.message.includes('force=true')) {
        const ok = await confirmDialog({
          title: '检测到高相似内容',
          message: e.message.replace('，确认仍要发布请带 force=true', '。确认仍要发布？'),
          confirmText: '仍要发布',
          danger: true,
        })
        if (ok) return create(true)
        return
      }
      toast.error(e.message || '创建失败')
    }
  }

  const cancel = async (task: PublishTask) => {
    if (!(await confirmDialog({ title: '取消该发布任务？' }))) return
    try {
      await api.cancelPublishTask(task.id)
      load()
    } catch (e: any) {
      toast.error(e.message || '取消失败')
    }
  }

  const retry = async (task: PublishTask) => {
    try {
      await api.retryPublishTask(task.id)
      toast.success('已重新排队')
      load()
    } catch (e: any) {
      toast.error(e.message || '重试失败')
    }
  }

  const exportPack = async (task: PublishTask) => {
    try {
      const pack = await api.exportPublishTask(task.id)
      const text = [
        `平台：${PLATFORM_LABEL[pack.platform] || pack.platform}    账号：${pack.account_name}`,
        `\n【标题】\n${pack.title}`,
        `\n【正文】\n${pack.body}`,
        `\n【话题】\n${(pack.tags || []).map((t: string) => `#${t}`).join(' ')}`,
        pack.script ? `\n【口播脚本】\n${pack.script}` : '',
        pack.cover_text ? `\n【封面文案】\n${pack.cover_text}` : '',
        `\n【素材】\n${(pack.materials || []).map((m: any) => `${window.location.origin}${m.url}`).join('\n') || '无'}`,
      ].join('')
      await navigator.clipboard.writeText(text)
      toast.success('发布包已复制到剪贴板（素材链接在浏览器打开下载）')
    } catch (e: any) {
      toast.error(e.message || '导出失败')
    }
  }

  // 可选版本：后端已平铺（审批通过 + 合规通过），此处只补展示标签（含变体风格标识）
  const versionOptions = publishable.map((v) => ({
    id: v.version_id,
    platform: v.platform,
    label: publishableLabel(v),
  }))
  const selectedPlatform = versionOptions.find((v) => v.id === form.version_id)?.platform || ''
  const candidateAccounts = accounts.filter((a) => !selectedPlatform || a.platform === selectedPlatform)

  const columns: ColumnsType<PublishTask> = [
    {
      title: '平台',
      dataIndex: 'platform',
      width: 90,
      render: (value: string) => <Tag>{PLATFORM_LABEL[value] || value}</Tag>,
    },
    { title: '账号', dataIndex: 'account_name', width: 140 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 180,
      render: (value: PublishTask['status'], row) => (
        <Space size={4} wrap>
          <Tag color={STATUS_COLOR[value] || 'default'}>{STATUS_STYLE[value]?.label || value}</Tag>
          {row.retries > 0 ? <Text type="warning">重试 {row.retries} 次</Text> : null}
        </Space>
      ),
    },
    {
      title: '计划时间',
      dataIndex: 'scheduled_at',
      width: 120,
      render: (value: string) => dayjs(value).format('MM-DD HH:mm'),
    },
    {
      title: '发布时间',
      dataIndex: 'published_at',
      width: 120,
      render: (value?: string | null) => (value ? dayjs(value).format('MM-DD HH:mm') : '—'),
    },
    {
      title: '结果',
      render: (_, task) => {
        if (!task.error && !task.post_url) return '—'
        return (
          <Space direction="vertical" size={4}>
            {task.error ? <Text type="danger">{task.error}</Text> : null}
            {task.post_url ? (
              <Link href={task.post_url} target="_blank" rel="noreferrer">
                {task.post_url}
              </Link>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: '操作',
      width: 280,
      render: (_, task) => (
        <Space wrap>
          <Button size="small" icon={<Download size={12} />} onClick={() => exportPack(task)}>
            导出发布包
          </Button>
          {task.status === 'failed' && (
            <Button size="small" icon={<RotateCcw size={12} />} onClick={() => retry(task)}>
              重试
            </Button>
          )}
          {(task.status === 'pending' || task.status === 'failed') && (
            <Button size="small" danger icon={<XCircle size={12} />} onClick={() => cancel(task)}>
              取消
            </Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div className="h-full flex flex-col bg-gray-50 dark:bg-gray-950">
      <div className="bg-white dark:bg-gray-900 border-b dark:border-gray-700 px-6 py-4 flex items-center gap-4">
        <div className="min-w-0">
          <Title level={4} style={{ margin: 0 }}>发布计划</Title>
          <Text type="secondary">
            定时发布到矩阵账号；自动发布总开关在「设置」页（默认关闭，可先导出发布包人工发）
          </Text>
        </div>
        <span className="flex-1" />
        <Space>
          <Space.Compact>
            <Button
              type={view === 'list' ? 'primary' : 'default'}
              icon={<List size={13} />}
              onClick={() => setView('list')}
            >
              列表
            </Button>
            <Button
              type={view === 'calendar' ? 'primary' : 'default'}
              icon={<CalendarDays size={13} />}
              onClick={() => setView('calendar')}
            >
              日历
            </Button>
          </Space.Compact>
          <Button type="primary" icon={<Plus size={15} />} onClick={openCreate}>
            新建发布任务
          </Button>
        </Space>
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        {view === 'calendar' ? (
          <CalendarGrid
            month={month}
            days={calDays}
            loading={calLoading}
            onMonthChange={setMonth}
            onDropTask={dropToDay}
          />
        ) : (
          <Table
            className="max-w-5xl"
            rowKey="id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={tasks}
            pagination={false}
            locale={{
              emptyText: (
                <Empty icon={CalendarClock} title="暂无发布任务" hint="先在「创作」页生成内容并审核通过" />
              ),
            }}
          />
        )}
      </div>

      <Modal
        open={showCreate}
        title="新建发布任务"
        width={460}
        onCancel={() => setShowCreate(false)}
        destroyOnClose
        footer={
          <Space>
            <Button onClick={() => setShowCreate(false)}>取消</Button>
            <Button
              icon={<Zap size={12} />}
              title="按账号时段位自动占坑（账号需开启队列模式）"
              onClick={() => enqueue()}
              className="!text-amber-700 !bg-amber-100 !border-transparent dark:!text-amber-400 dark:!bg-amber-900/40"
            >
              加入队列
            </Button>
            <Button type="primary" onClick={() => create()}>
              创建
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} className="w-full">
          <div className="w-full">
            <Text type="secondary">内容版本（仅显示已通过审批且合规通过的版本）</Text>
            {publishable.length === 0 ? (
              <div className="mt-1 border border-dashed dark:border-gray-600 rounded-lg p-3 text-xs text-gray-500 dark:text-gray-400 space-y-1.5">
                <div className="font-medium text-gray-600 dark:text-gray-300">没有可发布的内容</div>
                <div>内容需满足三道闸才能发布：</div>
                <div>① AI 生成版本 → ② 全部版本合规通过 → ③ 提交审核并由管理员审批通过</div>
                <Button
                  type="link"
                  size="small"
                  className="!px-0"
                  onClick={() => { setShowCreate(false); navigate('/contents') }}
                >
                  → 去创作台处理
                </Button>
              </div>
            ) : (
              <Select
                className="mt-1"
                style={{ width: '100%' }}
                value={form.version_id || undefined}
                placeholder="请选择"
                options={versionOptions.map((v) => ({ value: v.id, label: v.label }))}
                onChange={(value) => setForm({ ...form, version_id: value, account_ids: [] })}
              />
            )}
          </div>
          <div className="w-full">
            <Text type="secondary">
              目标账号{selectedPlatform ? `（${PLATFORM_LABEL[selectedPlatform]}）` : ''}（可多选）
            </Text>
            {candidateAccounts.length === 0 ? (
              <div className="mt-1 text-xs text-gray-400 py-2 text-center border dark:border-gray-600 rounded-lg">
                无可用账号（需与内容平台匹配且状态正常）
              </div>
            ) : (
              <Select
                mode="multiple"
                allowClear
                className="mt-1"
                style={{ width: '100%' }}
                value={form.account_ids}
                placeholder="选择账号"
                options={candidateAccounts.map((a) => ({
                  value: a.id,
                  label: `${a.account_name}（今日 ${a.today_published}/${a.daily_publish_limit}）`,
                }))}
                onChange={(ids) => setForm({ ...form, account_ids: ids })}
              />
            )}
          </div>
          <div className="w-full">
            <Text type="secondary">计划时间（留空 = 立即执行）</Text>
            <DatePicker
              className="mt-1"
              style={{ width: '100%' }}
              showTime={{ format: 'HH:mm' }}
              format="YYYY-MM-DD HH:mm"
              value={form.scheduled_at ? dayjs(form.scheduled_at) : null}
              placeholder="留空则立即执行"
              onChange={(value) => {
                setForm({
                  ...form,
                  scheduled_at: value && !Array.isArray(value) ? value.format('YYYY-MM-DDTHH:mm') : '',
                })
              }}
            />
            {bestSlotHint && (
              <Text type="warning" className="block mt-1">{bestSlotHint}</Text>
            )}
          </div>
        </Space>
      </Modal>
    </div>
  )
}

/** 月历格子视图：任务按状态着色，待发布任务可拖拽改期（保留原时刻）。 */
function CalendarGrid({
  month, days, loading, onMonthChange, onDropTask,
}: {
  month: string
  days: CalendarDay[]
  loading: boolean
  onMonthChange: (m: string) => void
  onDropTask: (date: string) => void
}) {
  const monthStart = dayjs(`${month}-01`)
  const firstCell = monthStart.subtract((monthStart.day() + 6) % 7, 'day') // 周一开头
  const cells = Array.from({ length: 42 }, (_, i) => firstCell.add(i, 'day'))
  const byDate = new Map(days.map((d) => [d.date, d.tasks]))
  const [dragOver, setDragOver] = useState('')

  return (
    <div className="max-w-6xl">
      <div className="flex items-center gap-3 mb-3">
        <Button
          icon={<ChevronLeft size={15} />}
          aria-label="上个月"
          onClick={() => onMonthChange(monthStart.subtract(1, 'month').format('YYYY-MM'))}
        />
        <span className="text-sm font-medium text-gray-800 dark:text-gray-100">
          {monthStart.format('YYYY 年 M 月')}
        </span>
        <Button
          icon={<ChevronRight size={15} />}
          aria-label="下个月"
          onClick={() => onMonthChange(monthStart.add(1, 'month').format('YYYY-MM'))}
        />
        <span className="text-xs text-gray-400 ml-2">拖拽「待发布」任务到其他日期即可改期（保留原时刻）</span>
      </div>
      <div className="grid grid-cols-7 gap-1 text-center text-xs text-gray-400 mb-1">
        {['一', '二', '三', '四', '五', '六', '日'].map((d) => <div key={d}>周{d}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-1">
        {cells.map((cell) => {
          const key = cell.format('YYYY-MM-DD')
          const dayTasks = byDate.get(key) || []
          const inMonth = cell.month() === monthStart.month()
          const isToday = key === dayjs().format('YYYY-MM-DD')
          return (
            <div
              key={key}
              onDragOver={(e) => { e.preventDefault(); setDragOver(key) }}
              onDragLeave={() => setDragOver('')}
              onDrop={(e) => { e.preventDefault(); setDragOver(''); onDropTask(key) }}
              className={clsx(
                'min-h-[96px] rounded-lg border p-1.5 transition-colors',
                inMonth ? 'bg-white dark:bg-gray-900 border-gray-100 dark:border-gray-800' : 'bg-gray-50 dark:bg-gray-950 border-transparent',
                isToday && 'ring-1 ring-blue-500',
                dragOver === key && 'border-blue-400 bg-blue-50 dark:bg-blue-900/20',
              )}
            >
              <div className={clsx('text-xs mb-1', inMonth ? 'text-gray-600 dark:text-gray-300' : 'text-gray-300 dark:text-gray-600')}>
                {cell.date()}
              </div>
              <div className="space-y-1">
                {dayTasks.map((t) => (
                  <div
                    key={t.id}
                    draggable={t.status === 'pending'}
                    onDragStart={() => sessionStorage.setItem('drag_task_id', String(t.id))}
                    title={`${t.account_name} ${dayjs(t.scheduled_at).format('HH:mm')} ${STATUS_STYLE[t.status]?.label || t.status}`}
                    className={clsx(
                      'text-[10px] rounded px-1.5 py-0.5 truncate',
                      STATUS_STYLE[t.status]?.cls,
                      t.status === 'pending' && 'cursor-grab active:cursor-grabbing',
                    )}
                  >
                    {dayjs(t.scheduled_at).format('HH:mm')} {t.account_name}
                  </div>
                ))}
                {loading && inMonth && dayTasks.length === 0 && <div className="h-3" />}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
