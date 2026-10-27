import { Button, Card, Form, Input, Modal, Select, Space, Spin, Tag, Typography } from 'antd'
import dayjs from 'dayjs'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, InspirationItem } from '../api/client'
import { confirmDialog } from '../components/ui/dialogs'
import { toast } from '../components/ui/toast'

const PLATFORM_LABEL: Record<string, string> = { douyin: '抖音', xiaohongshu: '小红书' }
const STATUS_LABEL: Record<string, { label: string; color: string }> = {
  new: { label: '新采集', color: 'blue' },
  analyzed: { label: '已拆解', color: 'purple' },
  used: { label: '已仿写', color: 'green' },
}

export default function Inspiration() {
  const navigate = useNavigate()
  const [items, setItems] = useState<InspirationItem[]>([])
  const [loading, setLoading] = useState(true)
  const [platform, setPlatform] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [busyId, setBusyId] = useState(0)
  const [form, setForm] = useState({ platform: 'xiaohongshu', title: '', source_url: '', author: '', content_text: '' })

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setItems(await api.listInspiration({ platform: platform || undefined }))
    } catch (e: any) {
      toast.error(e.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [platform])

  useEffect(() => {
    load()
  }, [load])

  const create = async () => {
    if (!form.title.trim()) {
      toast.error('标题必填')
      return
    }
    try {
      await api.createInspiration(form)
      toast.success('已录入灵感库')
      setShowCreate(false)
      setForm({ platform: 'xiaohongshu', title: '', source_url: '', author: '', content_text: '' })
      load()
    } catch (e: any) {
      toast.error(e.message || '录入失败')
    }
  }

  const analyze = async (item: InspirationItem) => {
    setBusyId(item.id)
    try {
      await api.analyzeInspiration(item.id)
      toast.success('AI 拆解完成')
      setExpanded(item.id)
      load()
    } catch (e: any) {
      toast.error(e.message || '拆解失败')
    } finally {
      setBusyId(0)
    }
  }

  const imitate = async (item: InspirationItem) => {
    setBusyId(item.id)
    try {
      const r = await api.imitateInspiration(item.id)
      toast.success('已创建仿写草稿，跳转到创作台生成内容')
      navigate(`/contents?item=${r.content_item_id}`)
    } catch (e: any) {
      toast.error(e.message || '仿写失败')
      setBusyId(0)
    }
  }

  const remove = async (item: InspirationItem) => {
    if (!(await confirmDialog({ title: '删除该灵感？', danger: true }))) return
    try {
      await api.deleteInspiration(item.id)
      load()
    } catch (e: any) {
      toast.error(e.message || '删除失败')
    }
  }

  return (
    <div className="h-full overflow-auto p-6">
      <div className="flex items-center gap-3 mb-4">
        <div className="flex-1">
          <Typography.Title level={4} style={{ marginBottom: 4 }}>爆款灵感库</Typography.Title>
          <Typography.Text type="secondary">手动录入或热榜采集后，AI 拆解标题公式、结构和钩子，再一键仿写</Typography.Text>
        </div>
        <Select
          value={platform}
          onChange={setPlatform}
          style={{ width: 140 }}
          options={[
            { value: '', label: '全部平台' },
            { value: 'xiaohongshu', label: '小红书' },
            { value: 'douyin', label: '抖音' },
          ]}
        />
        <Button type="primary" onClick={() => setShowCreate(true)}>手动录入</Button>
      </div>
      <Spin spinning={loading}>
        {items.length === 0 && !loading ? (
          <Typography.Text type="secondary">灵感库为空。可以手动录入，或运行热榜采集。</Typography.Text>
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%', maxWidth: 960 }}>
            {items.map((item) => (
              <Card key={item.id} size="small">
                <Space wrap>
                  <Tag color="blue">{PLATFORM_LABEL[item.platform] || item.platform}</Tag>
                  <Tag>{item.source === 'rpa' ? `热榜采集${item.keyword ? ` · ${item.keyword}` : ''}` : '手动录入'}</Tag>
                  <Tag color={STATUS_LABEL[item.status]?.color}>{STATUS_LABEL[item.status]?.label || item.status}</Tag>
                  {(item.stats_json?.digg ?? 0) > 0 && <Tag color="orange">点赞 {item.stats_json.digg.toLocaleString()}</Tag>}
                  <Typography.Text type="secondary">{dayjs(item.created_at).format('MM-DD HH:mm')}</Typography.Text>
                </Space>
                <div className="mt-2 font-medium">{item.title}</div>
                <Typography.Text type="secondary">
                  {item.author ? `作者：${item.author} · ` : ''}
                  {item.source_url ? <Typography.Link href={item.source_url} target="_blank">原文链接</Typography.Link> : null}
                </Typography.Text>
                {expanded === item.id && item.analysis && Object.keys(item.analysis).length > 0 && (
                  <Card size="small" className="!mt-3" title="AI 拆解结论">
                    {item.analysis.title_formula && <div>标题公式：{item.analysis.title_formula}</div>}
                    {item.analysis.structure && <div>内容结构：{item.analysis.structure}</div>}
                    {(item.analysis.hooks || []).length > 0 && <div>开头钩子：{item.analysis.hooks!.join('；')}</div>}
                    {item.analysis.selling_angle && <div>卖点切入：{item.analysis.selling_angle}</div>}
                    {item.analysis.why_viral && <div>爆款原因：{item.analysis.why_viral}</div>}
                    {(item.analysis.reusable_points || []).length > 0 && <div>可复用要点：{item.analysis.reusable_points!.join('；')}</div>}
                  </Card>
                )}
                {expanded === item.id && item.content_text && (
                  <Typography.Paragraph className="!mt-2 whitespace-pre-wrap" type="secondary">{item.content_text}</Typography.Paragraph>
                )}
                <Space className="mt-3" wrap>
                  <Button size="small" onClick={() => setExpanded(expanded === item.id ? null : item.id)}>
                    {expanded === item.id ? '收起' : '详情'}
                  </Button>
                  <Button size="small" loading={busyId === item.id} onClick={() => analyze(item)}>
                    {item.status === 'new' ? 'AI 拆解' : '重新拆解'}
                  </Button>
                  <Button size="small" type="primary" disabled={busyId === item.id} onClick={() => imitate(item)}>一键仿写</Button>
                  <Button size="small" danger onClick={() => remove(item)}>删除</Button>
                </Space>
              </Card>
            ))}
          </Space>
        )}
      </Spin>
      <Modal
        open={showCreate}
        title="手动录入爆款"
        okText="录入"
        cancelText="取消"
        onCancel={() => setShowCreate(false)}
        onOk={create}
        destroyOnClose
      >
        <Form layout="vertical">
          <Form.Item label="平台">
            <Select
              value={form.platform}
              onChange={(value) => setForm({ ...form, platform: value })}
              options={[{ value: 'xiaohongshu', label: '小红书' }, { value: 'douyin', label: '抖音' }]}
            />
          </Form.Item>
          <Form.Item label="标题" required>
            <Input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          </Form.Item>
          <Form.Item label="原文链接">
            <Input value={form.source_url} onChange={(e) => setForm({ ...form, source_url: e.target.value })} />
          </Form.Item>
          <Form.Item label="作者">
            <Input value={form.author} onChange={(e) => setForm({ ...form, author: e.target.value })} />
          </Form.Item>
          <Form.Item label="正文/口播文案">
            <Input.TextArea rows={5} value={form.content_text} onChange={(e) => setForm({ ...form, content_text: e.target.value })} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
