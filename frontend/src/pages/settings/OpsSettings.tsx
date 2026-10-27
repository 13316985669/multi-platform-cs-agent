import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { Button, Card, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { api, AuditLogItem, QueueFailedTask, QueueOpsOverview } from '../../api/client'
import { toast } from '../../components/ui/toast'
import { useAuth } from '../../store'

const TASK_TYPE_LABELS: Record<string, string> = {
  inbound_message: '私信入站',
  inbound_comment: '评论入站',
  content_generate: '内容生成',
  first_comment: '首评引流',
}

const ACTION_LABELS: Record<string, string> = {
  takeover: '接管会话',
  release: '释放会话',
  close: '关闭会话',
  kb_faq_create: '新增 FAQ',
  kb_faq_update: '修改 FAQ',
  kb_doc_delete: '删除文档',
  kb_doc_upload: '上传文档',
  kb_dialog_import: '导入历史对话',
  ai_switch: 'AI 总开关',
  auto_switches: '自动化开关',
  brand_style_update: '品牌语气更新',
  rpa_outbox_retry: '重发失败消息',
  rpa_outbox_discard: '忽略失败消息',
}

export default function OpsSettings() {
  const { agent: me } = useAuth()
  const [auditLogs, setAuditLogs] = useState<AuditLogItem[]>([])
  const [queueOps, setQueueOps] = useState<QueueOpsOverview | null>(null)

  const load = async () => {
    try {
      setAuditLogs((await api.listAuditLogs()).items)
    } catch { /* 忽略 */ }
    try {
      setQueueOps(await api.queueFailed())
    } catch { /* 忽略 */ }
  }

  useEffect(() => {
    if (me?.role !== 'admin') return
    load()
  }, [me?.role])

  if (me?.role !== 'admin') {
    return <Navigate to="/settings/service" replace />
  }

  const downloadJson = (filename: string, data: unknown, message: string) => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = filename
    a.click()
    URL.revokeObjectURL(a.href)
    toast.success(message)
  }

  const queueColumns: ColumnsType<QueueFailedTask> = [
    {
      title: '任务',
      dataIndex: 'task_type',
      width: 140,
      render: (value: string) => <Tag color="blue">{TASK_TYPE_LABELS[value] || value}</Tag>,
    },
    {
      title: '编号',
      dataIndex: 'id',
      width: 160,
      render: (value: number, row) => `#${value} · 重试 ${row.retries} 次`,
    },
    {
      title: '错误',
      dataIndex: 'error',
      ellipsis: true,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 180,
      render: (value: string | null) => (value ? new Date(value).toLocaleString() : ''),
    },
    {
      key: 'actions',
      title: '操作',
      width: 80,
      align: 'right',
      render: (_, row) => (
        <Button
          type="link"
          size="small"
          onClick={async () => {
            try {
              await api.retryQueueTask(row.id)
              toast.success('已重新入队')
              load()
            } catch (e: any) {
              toast.error(e.message || '重试失败')
            }
          }}
        >
          重试
        </Button>
      ),
    },
  ]

  const auditColumns: ColumnsType<AuditLogItem> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 180,
      render: (value: string) => new Date(value).toLocaleString(),
    },
    { title: '操作人', dataIndex: 'agent_name', width: 120 },
    {
      title: '动作',
      dataIndex: 'action',
      width: 160,
      render: (value: string) => <Tag color="blue">{ACTION_LABELS[value] || value}</Tag>,
    },
    {
      key: 'detail',
      title: '详情',
      ellipsis: true,
      render: (_, row) => `${row.target} ${row.detail}`,
    },
  ]

  return (
    <div className="max-w-5xl">
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card
          title="队列运维（后台任务）"
          extra={queueOps ? (
            <span className="text-xs text-gray-400">
              待处理 {queueOps.pending} ·
              <span className={queueOps.failed > 0 ? 'text-red-500 font-medium' : ''}> 失败 {queueOps.failed}</span>
            </span>
          ) : null}
        >
          <div className="text-xs text-gray-400 mb-3">
            私信/评论入站走 fast 通道，内容生成/发布走 slow 通道，互不阻塞；失败任务自动退避重试 3 次（30s/60s/120s）后停在这里。
          </div>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            columns={queueColumns}
            dataSource={queueOps?.items ?? []}
            locale={{ emptyText: '暂无失败任务' }}
            scroll={{ y: 224 }}
          />
        </Card>

        <Card title="操作审计日志">
          <Space wrap style={{ marginBottom: 12 }}>
            <Button
              type="link"
              size="small"
              onClick={async () => {
                const cases = await api.exportBadCases()
                downloadJson('badcase-cases.json', cases, `已导出 ${cases.length} 条 badcase`)
              }}
            >
              导出 badcase（evals 格式，人工审阅后合入 backend/evals/cases.json）
            </Button>
            <Button
              type="link"
              size="small"
              onClick={async () => {
                const cases = await api.draftEvalCases()
                downloadJson('eval-drafts.json', cases, `已生成 ${cases.length} 条评测草稿`)
              }}
            >
              生成评测草稿
            </Button>
          </Space>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            columns={auditColumns}
            dataSource={auditLogs}
            locale={{ emptyText: '暂无操作记录' }}
            scroll={{ y: 256 }}
          />
        </Card>
      </Space>
    </div>
  )
}
