import { Button, Card, Input, Select, Space, Switch, Tabs, Tag, Typography } from 'antd'
import dayjs from 'dayjs'
import { MessageCircle, Plus, Settings2, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { api, AutoSwitches, CommentRule, PostComment } from '../api/client'
import Empty from '../components/ui/Empty'
import Skeleton from '../components/ui/Skeleton'
import { confirmDialog, promptDialog } from '../components/ui/dialogs'
import { toast } from '../components/ui/toast'
import { subscribeWs, useAuth } from '../store'

const { Text } = Typography

const INTENT_STYLE: Record<string, { label: string; color: string }> = {
  consult: { label: '咨询', color: 'blue' },
  price: { label: '问价', color: 'purple' },
  praise: { label: '好评', color: 'green' },
  complaint: { label: '差评', color: 'red' },
  spam: { label: '广告', color: 'orange' },
  irrelevant: { label: '无关', color: 'default' },
}

const STATUS_STYLE: Record<string, { label: string; color: string }> = {
  pending: { label: '待处理', color: 'gold' },
  replied: { label: '已自动回复', color: 'green' },
  manual: { label: '已人工回复', color: 'blue' },
  skipped: { label: '已跳过', color: 'default' },
}

const PLATFORM_LABEL: Record<string, string> = { douyin: '抖音', xiaohongshu: '小红书' }

export default function Comments() {
  const { agent } = useAuth()
  const isAdmin = agent?.role === 'admin'
  const [tab, setTab] = useState<'list' | 'rules'>('list')
  const [statusFilter, setStatusFilter] = useState('pending')
  const [comments, setComments] = useState<PostComment[]>([])
  const [rules, setRules] = useState<CommentRule[]>([])
  const [switches, setSwitches] = useState<AutoSwitches | null>(null)
  const [loading, setLoading] = useState(true)
  const [replyingId, setReplyingId] = useState(0)
  const [replyText, setReplyText] = useState('')

  const load = useCallback(async () => {
    try {
      const [cs, rs, sw] = await Promise.all([
        api.listComments(statusFilter ? { status: statusFilter } : {}),
        api.listCommentRules(),
        api.getAutoSwitches().catch(() => null),
      ])
      setComments(cs)
      setRules(rs)
      setSwitches(sw)
    } catch (e: any) {
      toast.error(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [statusFilter])

  useEffect(() => {
    load()
  }, [load])

  useEffect(
    () =>
      subscribeWs((event) => {
        if (event === 'comment_new') load()
      }),
    [load],
  )

  const reply = async (id: number) => {
    if (!replyText.trim()) return
    try {
      await api.replyComment(id, replyText.trim())
      toast.success('已回复')
      setReplyingId(0)
      setReplyText('')
      load()
    } catch (e: any) {
      toast.error(e.message || '回复失败')
    }
  }

  const skip = async (id: number) => {
    try {
      await api.skipComment(id)
      load()
    } catch (e: any) {
      toast.error(e.message || '操作失败')
    }
  }

  const toggleAuto = async () => {
    if (!switches) return
    const next = !switches.comment_auto_reply_enabled
    try {
      const updated = await api.setAutoSwitches({ comment_auto_reply_enabled: next })
      setSwitches(updated)
      toast.success(next ? '评论自动回复已开启' : '评论自动回复已关闭（全部转人工）')
    } catch (e: any) {
      toast.error(e.message || '切换失败')
    }
  }

  const addRule = async () => {
    const intent = await promptDialog({
      title: '命中意图（留空=仅按关键词）',
      placeholder: 'consult | price | praise',
    })
    if (intent === null) return
    const keywords = await promptDialog({ title: '关键词（逗号分隔，可留空）', placeholder: '如：多少钱,怎么买' })
    if (keywords === null) return
    const templates = await promptDialog({
      title: '回复模板（| 分隔多条，{code} 为暗号占位）',
      placeholder: '如：已私您啦|私信我哈',
    })
    if (!templates?.trim()) {
      toast.error('至少一条回复模板')
      return
    }
    const code = await promptDialog({ title: '暗号（用户私信该词完成归因，可留空）', placeholder: '如：价格' })
    try {
      await api.createCommentRule({
        intent: (intent || '').trim(),
        keywords: (keywords || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        reply_templates: templates.split('|').map((s) => s.trim()).filter(Boolean),
        guide_code: (code || '').trim(),
        enabled: true,
        priority: 10,
      })
      toast.success('规则已创建')
      load()
    } catch (e: any) {
      toast.error(e.message || '创建失败')
    }
  }

  const toggleRule = async (rule: CommentRule) => {
    try {
      await api.updateCommentRule(rule.id, { ...rule, enabled: !rule.enabled })
      load()
    } catch (e: any) {
      toast.error(e.message || '操作失败')
    }
  }

  const removeRule = async (rule: CommentRule) => {
    if (!(await confirmDialog({ title: '删除该规则？', danger: true }))) return
    try {
      await api.deleteCommentRule(rule.id)
      load()
    } catch (e: any) {
      toast.error(e.message || '删除失败')
    }
  }

  return (
    <div className="h-full flex flex-col bg-gray-50 dark:bg-gray-950">
      <div className="bg-white dark:bg-gray-900 border-b dark:border-gray-700 px-6 py-4 flex items-center">
        <div>
          <h1 className="text-lg font-medium text-gray-800 dark:text-gray-100">评论管理</h1>
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
            评论采集 → 意图识别 → 自动回复（暗号引导私信）；差评/广告自动转人工
          </p>
        </div>
        <span className="flex-1" />
        {isAdmin && switches && (
          <Space>
            <span className="text-xs text-gray-600 dark:text-gray-300">
              自动回复：{switches.comment_auto_reply_enabled ? '已开启' : '已关闭'}
            </span>
            <Switch checked={switches.comment_auto_reply_enabled} onChange={() => { toggleAuto() }} />
          </Space>
        )}
      </div>

      <div className="bg-white dark:bg-gray-900 border-b dark:border-gray-700 px-6">
        <Tabs
          className="[&_.ant-tabs-content-holder]:hidden"
          activeKey={tab}
          onChange={(key) => {
            if (key === 'list' || key === 'rules') setTab(key)
          }}
          tabBarStyle={{ marginBottom: 0 }}
          tabBarExtraContent={tab === 'list' ? (
            <Select
              size="small"
              value={statusFilter}
              onChange={setStatusFilter}
              style={{ width: 140 }}
              options={[
                { value: '', label: '全部状态' },
                { value: 'pending', label: '待处理' },
                { value: 'replied', label: '已自动回复' },
                { value: 'manual', label: '已人工回复' },
                { value: 'skipped', label: '已跳过' },
              ]}
            />
          ) : null}
          items={[
            { key: 'list', label: '评论列表' },
            {
              key: 'rules',
              label: (
                <span className="inline-flex items-center gap-1">
                  <Settings2 size={13} />
                  回复规则
                </span>
              ),
            },
          ]}
        />
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        {tab === 'list' ? (
          loading ? (
            <Skeleton rows={4} className="max-w-4xl" />
          ) : comments.length === 0 ? (
            <Empty icon={MessageCircle} title="暂无评论" hint="发布内容后，评论会自动采集到这里" />
          ) : (
            <Space direction="vertical" size={12} style={{ width: '100%', maxWidth: 896 }}>
              {comments.map((c) => (
                <Card key={c.id} size="small" style={{ width: '100%' }}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <Tag>{PLATFORM_LABEL[c.platform] || c.platform}</Tag>
                    <Text type="secondary">{c.author_nickname}</Text>
                    {c.intent ? (
                      <Tag color={INTENT_STYLE[c.intent]?.color}>
                        {INTENT_STYLE[c.intent]?.label || c.intent}
                      </Tag>
                    ) : null}
                    <Tag color={STATUS_STYLE[c.status]?.color}>
                      {STATUS_STYLE[c.status]?.label || c.status}
                    </Tag>
                    <span className="flex-1" />
                    <Text type="secondary">{dayjs(c.created_at).format('MM-DD HH:mm')}</Text>
                  </div>
                  <div className="mt-2 text-sm text-gray-800 dark:text-gray-100">{c.content}</div>
                  {c.post_title ? (
                    <div className="mt-1"><Text type="secondary">作品：{c.post_title}</Text></div>
                  ) : null}
                  {c.reply_content ? (
                    <div className="mt-2 text-xs text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-900/20 rounded p-2">
                      回复：{c.reply_content}
                    </div>
                  ) : null}
                  {(c.status === 'pending' || c.status === 'skipped') && (
                    replyingId === c.id ? (
                      <div className="mt-3 flex gap-2">
                        <Input
                          autoFocus
                          style={{ flex: 1, minWidth: 0 }}
                          value={replyText}
                          onChange={(e) => setReplyText(e.target.value)}
                          placeholder="输入回复（禁止联系方式，违规将被拦截）"
                          onPressEnter={() => reply(c.id)}
                        />
                        <Button type="primary" onClick={() => reply(c.id)}>发送</Button>
                        <Button onClick={() => { setReplyingId(0); setReplyText('') }}>取消</Button>
                      </div>
                    ) : (
                      <Space className="mt-3" wrap>
                        <Button size="small" type="primary" ghost onClick={() => { setReplyingId(c.id); setReplyText('') }}>
                          人工回复
                        </Button>
                        {c.status === 'pending' && (
                          <Button size="small" onClick={() => skip(c.id)}>跳过</Button>
                        )}
                      </Space>
                    )
                  )}
                </Card>
              ))}
            </Space>
          )
        ) : (
          <div className="max-w-4xl">
            {isAdmin && (
              <Button type="primary" icon={<Plus size={15} />} onClick={addRule} className="mb-4">
                新建规则
              </Button>
            )}
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              {rules.map((rule) => (
                <Card key={rule.id} size="small" style={{ width: '100%' }}>
                  <div className="flex items-center gap-2 flex-wrap">
                    {rule.intent ? (
                      <Tag color={INTENT_STYLE[rule.intent]?.color}>
                        意图:{INTENT_STYLE[rule.intent]?.label || rule.intent}
                      </Tag>
                    ) : null}
                    {(rule.keywords || []).map((k) => (
                      <Tag key={k}>{k}</Tag>
                    ))}
                    {rule.guide_code ? (
                      <Tag color="purple">暗号:{rule.guide_code}</Tag>
                    ) : null}
                    <Text type="secondary">优先级 {rule.priority}</Text>
                    <span className="flex-1" />
                    {isAdmin && (
                      <Space size={4}>
                        <Switch size="small" checked={rule.enabled} onChange={() => { toggleRule(rule) }} />
                        <span className="text-xs">{rule.enabled ? '已启用' : '已停用'}</span>
                        <Button
                          type="text"
                          danger
                          size="small"
                          aria-label="删除"
                          icon={<Trash2 size={12} />}
                          onClick={() => removeRule(rule)}
                        />
                      </Space>
                    )}
                  </div>
                  <div className="mt-2">
                    {(rule.reply_templates || []).map((t, i) => (
                      <div key={i} className="text-xs text-gray-600 dark:text-gray-300">
                        模板{i + 1}：{t}
                      </div>
                    ))}
                  </div>
                </Card>
              ))}
            </Space>
            {rules.length === 0 && (
              <Empty icon={Settings2} title="暂无规则" hint="规则命中评论后自动回复模板并引导私信暗号" />
            )}
          </div>
        )}
      </div>
    </div>
  )
}
