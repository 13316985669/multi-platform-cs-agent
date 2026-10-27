import { useEffect, useState } from 'react'
import { Button, Card, Select, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { api, appBase, RpaFailedMessage, RpaWorker } from '../../api/client'
import { toast } from '../../components/ui/toast'
import { useAuth } from '../../store'

const INTEGRATIONS = [
  { key: 'llm', label: 'LLM（对话模型）' },
  { key: 'embedding', label: 'Embedding（向量）' },
  { key: 'rerank', label: 'Rerank（精排）' },
  { key: 'douyin', label: '抖音开放平台' },
  { key: 'xiaohongshu', label: '小红书开放平台' },
  { key: 'wecom', label: '企业微信 API' },
  { key: 'wecom_callback', label: '企微回调（加粉归因）' },
  { key: 'zhini', label: '知你快回回复接口' },
]

const WORKER_STATUS: Record<RpaWorker['status'], string | null> = {
  online: null,
  login_expired: '登录过期 → 请在 Worker 机器运行 python login.py',
  selector_mismatch: '页面改版 → 请更新 Worker 选择器（docs/rpa-workers.md）',
  offline: '已掉线 → 请检查 Worker 进程与网络',
}

export default function ChannelSettings() {
  const { agent } = useAuth()
  const isAdmin = agent?.role === 'admin'
  const [health, setHealth] = useState<Record<string, any>>({})
  const [rpaWorkers, setRpaWorkers] = useState<RpaWorker[]>([])
  const [rpaFailed, setRpaFailed] = useState<RpaFailedMessage[]>([])
  const [accountType, setAccountType] = useState('feige')

  const load = async () => {
    setHealth(await api.health())
    try {
      setRpaWorkers(await api.listRpaWorkers())
      setRpaFailed((await api.listRpaFailed()).items)
    } catch {
      setRpaWorkers([])
      setRpaFailed([])
    }
    try {
      setAccountType((await api.getDouyinAccountType()).account_type)
    } catch { /* 忽略 */ }
  }

  const ACCOUNT_TYPE_TOASTS: Record<string, string> = {
    feige: '已改为抖店飞鸽',
    enterprise: '已改为企业号，私信将打开 e.douyin.com',
    laike: '已改为抖音来客，私信将打开 life.douyin.com',
  }

  const changeAccountType = async (next: string) => {
    try {
      const saved = await api.setDouyinAccountType(next)
      setAccountType(saved.account_type)
      toast.success(ACCOUNT_TYPE_TOASTS[next] || `已改为 ${next}`)
    } catch (e: any) {
      toast.error(e.message || '保存失败')
    }
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 30000)
    return () => clearInterval(timer)
  }, [])

  const workerColumns: ColumnsType<RpaWorker> = [
    {
      title: '账号',
      dataIndex: 'account',
      render: (value: string, row) => (
        <Space>
          <span className={`inline-block w-2 h-2 rounded-full ${row.status === 'online' ? 'bg-green-500' : 'bg-red-500'}`} />
          <span>{value}</span>
        </Space>
      ),
    },
    {
      title: '通道',
      render: (_, row) => `${row.platform === 'douyin' ? '抖音' : '小红书'} · ${row.worker_id}`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      render: (value: RpaWorker['status']) => (
        WORKER_STATUS[value] ? <Tag color="error">{WORKER_STATUS[value]}</Tag> : <Tag color="success">在线</Tag>
      ),
    },
    { title: '待发', dataIndex: 'pending', width: 80 },
    {
      title: '失败',
      dataIndex: 'failed',
      width: 80,
      render: (value: number) => <span className={value > 0 ? 'text-red-500' : ''}>{value}</span>,
    },
    {
      title: '心跳',
      dataIndex: 'last_heartbeat_at',
      width: 120,
      render: (value: string) => new Date(value).toLocaleTimeString(),
    },
  ]

  const failedColumns: ColumnsType<RpaFailedMessage> = [
    {
      title: '消息',
      render: (_, row) => (
        <div className="min-w-0">
          <span className="text-gray-400">[{row.account}]</span>
          <span className="ml-1">{row.content || `（${row.msg_type}）`}</span>
          <div className="text-gray-400 truncate">{row.error}</div>
        </div>
      ),
    },
    {
      key: 'actions',
      title: '操作',
      width: 140,
      align: 'right',
      render: (_, row) => (
        <Space size={0}>
          <Button
            type="link"
            size="small"
            onClick={async () => { await api.retryRpaOutbox(row.outbox_id); toast.success('已重新入队'); load() }}
          >
            重发
          </Button>
          <Button
            type="link"
            size="small"
            onClick={async () => { await api.discardRpaOutbox(row.outbox_id); toast.info('已忽略该消息'); load() }}
          >
            忽略
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div className="max-w-5xl">
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card title="集成配置状态（在 backend/.env 中配置）">
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {INTEGRATIONS.map((item) => (
              <div key={item.key} className="flex items-center justify-between text-sm">
                <span className="text-gray-600 dark:text-gray-300">{item.label}</span>
                <Tag color={health[item.key] ? 'green' : 'default'}>
                  {health[item.key] ? '已配置' : '未配置（降级运行）'}
                </Tag>
              </div>
            ))}
          </Space>
          <p className="mt-4 text-xs leading-5 text-gray-500 dark:text-gray-400">
            知你快回插件的接口地址填
            <code className="mx-1">{`${window.location.origin}${appBase()}/api/integrations/zhinikuaihui/reply`}</code>
            ，身份验证填 ZHINI_REPLY_API_KEY，等待时间选 60 秒。密钥只写在后端 .env，这里不显示明文。
          </p>
        </Card>

        <Card
          title="抖音账号类型"
          extra={(
            <Select
              value={accountType}
              disabled={!isAdmin}
              onChange={changeAccountType}
              style={{ width: 160 }}
              options={[
                { value: 'feige', label: '抖店飞鸽' },
                { value: 'enterprise', label: '企业号' },
                { value: 'laike', label: '抖音来客' },
              ]}
            />
          )}
        >
          <p className="text-xs text-gray-400 mb-4">这是默认形态：账号页没有单独指定时才用。企业号私信打开 e.douyin.com，来客私信打开 life.douyin.com。评论页不变。切换默认值只改写还没单独指定形态的私信绑定。</p>
          <div className="flex items-center justify-between mb-4">
            <div className="text-sm font-medium text-gray-700 dark:text-gray-200">RPA 通道（Worker 状态，按店铺账号分组）</div>
            <a href="https://github.com" className="text-xs text-blue-500 hover:underline" onClick={(e) => e.preventDefault()}>
              接入指南见 docs/rpa-workers.md
            </a>
          </div>
          {rpaWorkers.length === 0 ? (
            <div className="text-xs text-gray-400">
              暂无在线 Worker。RPA 通道用于无官方 API 资质时的降级接入：在商家电脑运行 workers/ 目录下的 Worker 即可。
            </div>
          ) : (
            <Table rowKey="worker_id" size="small" pagination={false} columns={workerColumns} dataSource={rpaWorkers} />
          )}

          {rpaFailed.length > 0 && (
            <div className="mt-4">
              <div className="text-xs font-medium text-red-600 mb-2">
                失败消息（{rpaFailed.length}）— 连续 3 次未发出的消息会停在这里，不会自动重试
              </div>
              <Table
                rowKey="outbox_id"
                size="small"
                pagination={false}
                columns={failedColumns}
                dataSource={rpaFailed}
                scroll={{ y: 192 }}
              />
            </div>
          )}
        </Card>
      </Space>
    </div>
  )
}
