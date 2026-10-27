import { ListChecks, MessageSquare, Users } from 'lucide-react'
import { ReactNode, useEffect, useState } from 'react'
import { Button, Card, Checkbox, Form, Input, Modal, Select, Space, Switch, Table, Tabs, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { api, Agent, BannedWord, QuickReply } from '../../api/client'
import { confirmDialog } from '../../components/ui/dialogs'
import Empty from '../../components/ui/Empty'
import { toast } from '../../components/ui/toast'
import { useAuth } from '../../store'
import {
  ALL_MODULE_KEYS,
  CS_MODULE_KEYS,
  MATRIX_MODULE_KEYS,
  MODULES,
  moduleSummary,
} from '../../utils/modules'

type Tab = 'agents' | 'quick' | 'banned'
type QuickDraft = { id: number | null; title: string; content: string; personal: boolean }
type BannedDraft = { id: number | null; word: string; category: string; direction: 'out' | 'in' }
type AgentDraft = {
  id: number | null
  username: string
  displayName: string
  password: string
  role: 'agent' | 'admin'
  maxConcurrent: number
  modules: string[]
  unrestricted: boolean
}

const STATUS_LABEL: Record<string, string> = {
  active: '接待中',
  resting: '休息中',
  offline: '离线',
}

function FormModal({
  title,
  submitText,
  wide,
  onClose,
  onSubmit,
  children,
}: {
  title: string
  submitText: string
  wide?: boolean
  onClose: () => void
  onSubmit: () => void
  children: ReactNode
}) {
  const [form] = Form.useForm()
  return (
    <Modal
      open
      title={title}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={submitText}
      cancelText="取消"
      width={wide ? 560 : 420}
    >
      <Form form={form} layout="vertical" requiredMark={false} onFinish={() => { void onSubmit() }}>
        <div className="max-h-[70vh] overflow-y-auto pr-1">{children}</div>
      </Form>
    </Modal>
  )
}

export default function ServiceSettings() {
  const { agent: me, loadMe } = useAuth()
  const isAdmin = me?.role === 'admin'
  const [tab, setTab] = useState<Tab>('agents')
  const [agents, setAgents] = useState<Agent[]>([])
  const [quickReplies, setQuickReplies] = useState<QuickReply[]>([])
  const [banned, setBanned] = useState<BannedWord[]>([])
  const [quickDraft, setQuickDraft] = useState<QuickDraft | null>(null)
  const [bannedDraft, setBannedDraft] = useState<BannedDraft | null>(null)
  const [agentDraft, setAgentDraft] = useState<AgentDraft | null>(null)

  const loadAgents = async () => setAgents(await api.listAgents())
  const loadQuick = async () => setQuickReplies(await api.listQuickReplies())
  const loadBanned = async () => {
    try {
      setBanned(await api.listBannedWords())
    } catch {
      setBanned([])
    }
  }

  useEffect(() => {
    if (tab === 'agents') loadAgents().catch(() => {})
    else if (tab === 'quick') loadQuick().catch(() => {})
    else loadBanned()
  }, [tab])

  const openCreate = () => {
    if (tab === 'agents') {
      setAgentDraft({
        id: null,
        username: '',
        displayName: '',
        password: '',
        role: 'agent',
        maxConcurrent: 20,
        modules: [...CS_MODULE_KEYS],
        unrestricted: false,
      })
    } else if (tab === 'quick') {
      setQuickDraft({ id: null, title: '', content: '', personal: false })
    } else {
      setBannedDraft({ id: null, word: '', category: '极限词', direction: 'out' })
    }
  }

  const saveAgent = async () => {
    if (!agentDraft) return
    const displayName = agentDraft.displayName.trim()
    const username = agentDraft.username.trim()
    const password = agentDraft.password.trim()
    if (!displayName) {
      toast.error('请填写姓名')
      return
    }
    if (agentDraft.maxConcurrent < 0 || Number.isNaN(agentDraft.maxConcurrent)) {
      toast.error('接待上限不能为负数')
      return
    }
    if (agentDraft.role === 'agent' && agentDraft.modules.length === 0) {
      toast.error('至少选择一个可见模块')
      return
    }
    const modules = agentDraft.role === 'agent' ? agentDraft.modules : undefined
    try {
      if (agentDraft.id == null) {
        if (!username || !password) {
          toast.error('请填写账号和密码')
          return
        }
        await api.createAgent(username, password, displayName, agentDraft.role, agentDraft.maxConcurrent, modules)
        toast.success('客服账号已创建')
      } else {
        await api.updateAgent(agentDraft.id, {
          display_name: displayName,
          role: agentDraft.role,
          max_concurrent: agentDraft.maxConcurrent,
          ...(password ? { password } : {}),
          ...(modules ? { modules } : {}),
        })
        toast.success('客服账号已保存')
        if (agentDraft.id === me?.id) await loadMe()
      }
      setAgentDraft(null)
      loadAgents()
    } catch (e: any) {
      toast.error(e.message || '保存失败')
    }
  }

  const removeAgent = async (a: Agent) => {
    const ok = await confirmDialog({
      title: `删除客服「${a.display_name}」？`,
      message: '删除后该账号无法登录，历史接待记录保留。',
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      await api.deleteAgent(a.id)
      toast.success('客服已删除')
      loadAgents()
    } catch (e: any) {
      toast.error(e.message || '删除失败')
    }
  }

  const saveQuick = async () => {
    if (!quickDraft) return
    const title = quickDraft.title.trim()
    const content = quickDraft.content.trim()
    if (!title || !content) {
      toast.error('请填写标题和内容')
      return
    }
    try {
      if (quickDraft.id == null) {
        await api.createQuickReply(title, content, quickDraft.personal)
        toast.success('快捷回复已添加')
      } else {
        await api.updateQuickReply(quickDraft.id, title, content, quickDraft.personal)
        toast.success('快捷回复已保存')
      }
      setQuickDraft(null)
      loadQuick()
    } catch (e: any) {
      toast.error(e.message || '保存失败')
    }
  }

  const removeQuick = async (q: QuickReply) => {
    const ok = await confirmDialog({
      title: `删除快捷回复「${q.title}」？`,
      message: '删除后工作台将不再显示这条模板。',
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      await api.deleteQuickReply(q.id)
      toast.info('已删除')
      loadQuick()
    } catch (e: any) {
      toast.error(e.message || '删除失败')
    }
  }

  const saveBanned = async () => {
    if (!bannedDraft) return
    const word = bannedDraft.word.trim()
    const category = bannedDraft.category.trim() || '极限词'
    if (!word) {
      toast.error('请填写违禁词')
      return
    }
    try {
      if (bannedDraft.id == null) {
        await api.addBannedWord(word, category, bannedDraft.direction)
        toast.success('已加入词库')
      } else {
        await api.updateBannedWord(bannedDraft.id, word, category, bannedDraft.direction)
        toast.success('违禁词已保存')
      }
      setBannedDraft(null)
      loadBanned()
    } catch (e: any) {
      toast.error(e.message || '保存失败')
    }
  }

  const removeBanned = async (w: BannedWord) => {
    const ok = await confirmDialog({
      title: `删除违禁词「${w.word}」？`,
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      await api.deleteBannedWord(w.id)
      toast.info('已删除')
      loadBanned()
    } catch (e: any) {
      toast.error(e.message || '删除失败')
    }
  }

  const canAdd = tab === 'quick' || isAdmin

  const agentColumns: ColumnsType<Agent> = [
    { title: '姓名', dataIndex: 'display_name' },
    { title: '账号', dataIndex: 'username', render: (value: string) => `@${value}` },
    {
      title: '角色',
      dataIndex: 'role',
      render: (value: string) => <Tag>{value === 'admin' ? '管理员' : '客服'}</Tag>,
    },
    { key: 'modules', title: '可见模块', render: (_, row) => moduleSummary(row) },
    {
      title: '状态',
      dataIndex: 'status',
      render: (value: string) => (
        <Tag color={value === 'active' ? 'green' : 'default'}>{STATUS_LABEL[value] || value}</Tag>
      ),
    },
    {
      title: '接待上限',
      dataIndex: 'max_concurrent',
      render: (value: number | undefined) => value ?? 20,
    },
  ]
  if (isAdmin) {
    agentColumns.push({
      key: 'actions',
      title: '操作',
      width: 140,
      align: 'right',
      render: (_, row) => (
        <Space size={0}>
          <Button
            type="link"
            size="small"
            onClick={() => setAgentDraft({
              id: row.id,
              username: row.username,
              displayName: row.display_name,
              password: '',
              role: row.role === 'admin' ? 'admin' : 'agent',
              maxConcurrent: row.max_concurrent ?? 20,
              modules: row.modules ?? [...ALL_MODULE_KEYS],
              unrestricted: row.role !== 'admin' && row.modules == null,
            })}
          >
            编辑
          </Button>
          {row.id !== me?.id && (
            <Button type="link" size="small" danger onClick={() => removeAgent(row)}>删除</Button>
          )}
        </Space>
      ),
    })
  }

  const quickColumns: ColumnsType<QuickReply> = [
    { title: '标题', dataIndex: 'title', width: 160 },
    { title: '内容', dataIndex: 'content', ellipsis: true },
    {
      title: '范围',
      dataIndex: 'agent_id',
      width: 90,
      render: (value: number | null | undefined) => <Tag>{value ? '我的' : '团队'}</Tag>,
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
            onClick={() => setQuickDraft({
              id: row.id,
              title: row.title,
              content: row.content,
              personal: !!row.agent_id,
            })}
          >
            编辑
          </Button>
          <Button type="link" size="small" danger onClick={() => removeQuick(row)}>删除</Button>
        </Space>
      ),
    },
  ]

  const bannedColumns: ColumnsType<BannedWord> = [
    { title: '词', dataIndex: 'word' },
    { title: '分类', dataIndex: 'category' },
    {
      title: '方向',
      dataIndex: 'direction',
      render: (value: string) => <Tag>{value === 'in' ? '入口' : '出口'}</Tag>,
    },
  ]
  if (isAdmin) {
    bannedColumns.push({
      key: 'actions',
      title: '操作',
      width: 140,
      align: 'right',
      render: (_, row) => (
        <Space size={0}>
          <Button
            type="link"
            size="small"
            onClick={() => setBannedDraft({
              id: row.id,
              word: row.word,
              category: row.category,
              direction: row.direction === 'in' ? 'in' : 'out',
            })}
          >
            编辑
          </Button>
          <Button type="link" size="small" danger onClick={() => removeBanned(row)}>删除</Button>
        </Space>
      ),
    })
  }

  return (
    <div className="max-w-5xl">
      <Card styles={{ body: { paddingTop: 0 } }}>
        <Tabs
          activeKey={tab}
          onChange={(key) => setTab(key as Tab)}
          tabBarExtraContent={canAdd ? (
            <Button type="primary" size="small" onClick={openCreate}>新增</Button>
          ) : null}
          items={[
            {
              key: 'agents',
              label: '客服账号',
              children: (
                <>
                  <div className="text-xs text-gray-400 mb-3">状态由客服本人在左下角切换。</div>
                  {agents.length === 0 ? (
                    <Empty icon={Users} title="暂无客服账号" />
                  ) : (
                    <Table rowKey="id" size="small" pagination={false} columns={agentColumns} dataSource={agents} />
                  )}
                </>
              ),
            },
            {
              key: 'quick',
              label: '快捷回复',
              children: (
                <>
                  <div className="text-xs text-gray-400 mb-3">团队模板所有客服可见；勾选「仅自己」后只在本人工作台出现。</div>
                  {quickReplies.length === 0 ? (
                    <Empty icon={MessageSquare} title="暂无快捷回复" hint="点击新增，写一条像真人说话的模板" />
                  ) : (
                    <Table rowKey="id" size="small" pagination={false} columns={quickColumns} dataSource={quickReplies} />
                  )}
                </>
              ),
            },
            {
              key: 'banned',
              label: '违禁词',
              children: (
                <>
                  <div className="text-xs text-gray-400 mb-3">出口词过滤 AI/客服回复中的极限词；入口词用于识别用户风险内容。</div>
                  {banned.length === 0 ? (
                    <Empty icon={ListChecks} title="暂无自定义违禁词" hint={isAdmin ? '点击新增，写入出口或入口词' : '管理员添加后会显示在这里'} />
                  ) : (
                    <Table rowKey="id" size="small" pagination={false} columns={bannedColumns} dataSource={banned} />
                  )}
                </>
              ),
            },
          ]}
        />
      </Card>

      {agentDraft && (
        <FormModal
          title={agentDraft.id == null ? '新增客服' : '编辑客服'}
          submitText={agentDraft.id == null ? '添加' : '保存'}
          wide
          onClose={() => setAgentDraft(null)}
          onSubmit={saveAgent}
        >
          {agentDraft.id == null ? (
            <Form.Item label="账号">
              <Input
                value={agentDraft.username}
                onChange={(e) => setAgentDraft({ ...agentDraft, username: e.target.value })}
                autoFocus
              />
            </Form.Item>
          ) : (
            <div className="text-xs text-gray-400 mb-3">账号 @{agentDraft.username}</div>
          )}
          <Form.Item label="姓名">
            <Input
              value={agentDraft.displayName}
              onChange={(e) => setAgentDraft({ ...agentDraft, displayName: e.target.value })}
              autoFocus={agentDraft.id != null}
            />
          </Form.Item>
          <Form.Item label={agentDraft.id == null ? '密码' : '新密码'}>
            <Input.Password
              value={agentDraft.password}
              onChange={(e) => setAgentDraft({ ...agentDraft, password: e.target.value })}
              placeholder={agentDraft.id == null ? '' : '留空则不修改'}
            />
          </Form.Item>
          <Form.Item label="角色">
            <Select
              value={agentDraft.role}
              disabled={agentDraft.id === me?.id}
              onChange={(value) => setAgentDraft({ ...agentDraft, role: value === 'admin' ? 'admin' : 'agent' })}
              options={[
                { value: 'agent', label: '客服' },
                { value: 'admin', label: '管理员' },
              ]}
            />
          </Form.Item>
          <Form.Item label="接待上限">
            <Input
              type="number"
              min={0}
              value={agentDraft.maxConcurrent}
              onChange={(e) => setAgentDraft({ ...agentDraft, maxConcurrent: Number(e.target.value) })}
            />
          </Form.Item>
          <div className="text-xs text-gray-500 dark:text-gray-400">
            <div className="flex items-center gap-2 mb-2">
              <span>可见模块</span>
              {agentDraft.role === 'agent' && (
                <Space size={4} style={{ marginLeft: 'auto' }}>
                  {(
                    [
                      ['客服组', CS_MODULE_KEYS],
                      ['矩阵组', MATRIX_MODULE_KEYS],
                      ['全选', ALL_MODULE_KEYS],
                    ] as const
                  ).map(([label, keys]) => (
                    <Button
                      key={label}
                      size="small"
                      htmlType="button"
                      onClick={() => setAgentDraft({ ...agentDraft, modules: [...keys], unrestricted: false })}
                    >
                      {label}
                    </Button>
                  ))}
                </Space>
              )}
            </div>
            {agentDraft.role === 'admin' ? (
              <p className="text-gray-400">管理员可见全部模块</p>
            ) : (
              <>
                {agentDraft.unrestricted && (
                  <p className="text-amber-600 dark:text-amber-400 mb-2">
                    该账号尚未单独配置，当前可见全部。保存后按勾选生效。
                  </p>
                )}
                <div className="space-y-2">
                  {(
                    [
                      ['客服', 'cs'],
                      ['矩阵', 'matrix'],
                      ['设置', 'settings'],
                    ] as const
                  ).map(([title, group]) => (
                    <div key={group}>
                      <div className="text-[11px] text-gray-400 mb-1">{title}</div>
                      <div className="grid grid-cols-4 gap-1.5">
                        {MODULES.filter((item) => item.group === group).map((item) => (
                          <Checkbox
                            key={item.key}
                            checked={agentDraft.modules.includes(item.key)}
                            onChange={() => {
                              const has = agentDraft.modules.includes(item.key)
                              const modules = has
                                ? agentDraft.modules.filter((key) => key !== item.key)
                                : [...agentDraft.modules, item.key]
                              setAgentDraft({ ...agentDraft, modules, unrestricted: false })
                            }}
                          >
                            {item.label}
                          </Checkbox>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        </FormModal>
      )}

      {quickDraft && (
        <FormModal
          title={quickDraft.id == null ? '新增快捷回复' : '编辑快捷回复'}
          submitText={quickDraft.id == null ? '添加' : '保存'}
          onClose={() => setQuickDraft(null)}
          onSubmit={saveQuick}
        >
          <Form.Item label="标题">
            <Input
              value={quickDraft.title}
              onChange={(e) => setQuickDraft({ ...quickDraft, title: e.target.value })}
              autoFocus
            />
          </Form.Item>
          <Form.Item label="内容">
            <Input.TextArea
              value={quickDraft.content}
              onChange={(e) => setQuickDraft({ ...quickDraft, content: e.target.value })}
              rows={4}
              placeholder="口语化，像真人说话"
            />
          </Form.Item>
          <Space>
            <Switch
              checked={quickDraft.personal}
              onChange={(checked) => setQuickDraft({ ...quickDraft, personal: checked })}
            />
            <span className="text-xs text-gray-500 dark:text-gray-400">仅自己</span>
          </Space>
        </FormModal>
      )}

      {bannedDraft && (
        <FormModal
          title={bannedDraft.id == null ? '新增违禁词' : '编辑违禁词'}
          submitText={bannedDraft.id == null ? '添加' : '保存'}
          onClose={() => setBannedDraft(null)}
          onSubmit={saveBanned}
        >
          <Form.Item label="违禁词">
            <Input
              value={bannedDraft.word}
              onChange={(e) => setBannedDraft({ ...bannedDraft, word: e.target.value })}
              autoFocus
            />
          </Form.Item>
          <Form.Item label="分类">
            <Input
              value={bannedDraft.category}
              onChange={(e) => setBannedDraft({ ...bannedDraft, category: e.target.value })}
              placeholder="极限词"
            />
          </Form.Item>
          <Form.Item label="方向">
            <Select
              value={bannedDraft.direction}
              onChange={(value) => setBannedDraft({ ...bannedDraft, direction: value === 'in' ? 'in' : 'out' })}
              options={[
                { value: 'out', label: '出口（过滤回复）' },
                { value: 'in', label: '入口（识别用户风险）' },
              ]}
            />
          </Form.Item>
        </FormModal>
      )}
    </div>
  )
}
