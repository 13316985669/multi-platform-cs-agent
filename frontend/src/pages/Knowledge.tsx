import { AutoComplete, Button, Card, Form, Input, Modal, Select, Space, Switch, Table, Tabs, Tag, Upload } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import clsx from 'clsx'
import { Download, FileText, FlaskConical, Plus, Upload as UploadIcon, X } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, downloadFaqPack, KnowledgeChunk, KnowledgeDoc, MissedQuestion, RecallTestResult } from '../api/client'
import { confirmDialog } from '../components/ui/dialogs'
import Empty from '../components/ui/Empty'
import { toast } from '../components/ui/toast'

type NavKey = 'all' | 'faq' | 'file' | 'missed'

const NAV: { key: NavKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'faq', label: 'FAQ' },
  { key: 'file', label: '文档' },
  { key: 'missed', label: '未命中' },
]

interface Draft {
  title: string
  category: string
  question: string
  answer: string
  similars: string[]
}

const EMPTY_DRAFT: Draft = { title: '', category: '未分类', question: '', answer: '', similars: [] }

function statusLabel(status: string) {
  if (status === 'active') return '启用'
  if (status === 'disabled') return '停用'
  return '已归档'
}

export default function Knowledge() {
  const location = useLocation()
  const navigate = useNavigate()
  const [docs, setDocs] = useState<KnowledgeDoc[]>([])
  const [missed, setMissed] = useState<MissedQuestion[]>([])
  const [nav, setNav] = useState<NavKey>('all')
  const [category, setCategory] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'disabled'>('all')
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [acting, setActing] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [creating, setCreating] = useState(false)
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  const [missedId, setMissedId] = useState<number | null>(null)
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([])
  const [chunksOpen, setChunksOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testOpen, setTestOpen] = useState(false)
  const [testQuery, setTestQuery] = useState('')
  const [testResult, setTestResult] = useState<RecallTestResult | null>(null)
  const [testing, setTesting] = useState(false)
  const [importing, setImporting] = useState(false)
  const [packImporting, setPackImporting] = useState(false)
  const [limit, setLimit] = useState(200)

  const refresh = async () => {
    const [list, pending] = await Promise.all([api.listDocs(), api.listMissed()])
    setDocs(list)
    setMissed(pending)
    return list
  }

  useEffect(() => {
    refresh()
  }, [])

  const visible = useMemo(
    () => docs.filter((d) => d.status !== 'archived'),
    [docs],
  )
  const categories = useMemo(() => {
    const names = new Set(visible.map((d) => d.category || '未分类'))
    return Array.from(names).sort()
  }, [visible])

  const filtered = useMemo(() => {
    const keyword = q.trim().toLowerCase()
    const isId = /^\d+$/.test(keyword)
    const rows = visible.filter((d) => {
      if (nav === 'faq' && d.doc_type !== 'faq') return false
      if (nav === 'file' && d.doc_type !== 'file') return false
      if (statusFilter !== 'all' && d.status !== statusFilter) return false
      if (category && (d.category || '未分类') !== category) return false
      if (!keyword) return true
      if (isId) return String(d.id).includes(keyword)
      const similars = (d.similar_questions || []).join(' ')
      const hay = `${d.title} ${d.question || ''} ${d.answer || ''} ${d.category || ''} ${similars}`.toLowerCase()
      return hay.includes(keyword)
    })
    return rows.sort((a, b) => b.id - a.id)
  }, [visible, nav, category, q, statusFilter])

  useEffect(() => {
    setLimit(200)
  }, [nav, category, q, statusFilter])

  const shown = useMemo(() => filtered.slice(0, limit), [filtered, limit])

  const activeDoc = visible.find((d) => d.id === selectedId) || null
  const counts = {
    all: visible.length,
    faq: visible.filter((d) => d.doc_type === 'faq').length,
    file: visible.filter((d) => d.doc_type === 'file').length,
    missed: missed.length,
  }

  const openDoc = async (doc: KnowledgeDoc) => {
    setCreating(false)
    setMissedId(null)
    setSelectedId(doc.id)
    setDraft({
      title: doc.title,
      category: doc.category || '未分类',
      question: doc.question || '',
      answer: doc.answer || '',
      similars: [...(doc.similar_questions || [])],
    })
    setChunksOpen(doc.doc_type === 'file')
    setChunks(await api.listChunks(doc.id).catch(() => []))
  }

  const startCreate = (prefill?: Partial<Draft>, fromMissed?: number) => {
    setNav(fromMissed ? 'missed' : 'faq')
    setSelectedId(null)
    setCreating(true)
    setMissedId(fromMissed ?? null)
    setChunks([])
    setDraft({ ...EMPTY_DRAFT, ...prefill })
  }

  useEffect(() => {
    const incoming = (location.state as { faqDraft?: Partial<Draft> } | null)?.faqDraft
    if (!incoming || (!incoming.question && !incoming.answer)) return
    startCreate({
      title: (incoming.title || incoming.question || '').slice(0, 30),
      question: incoming.question || '',
      answer: incoming.answer || '',
    })
    navigate('/knowledge', { replace: true, state: null })
  }, [location.state])

  const closeEditor = () => {
    setSelectedId(null)
    setCreating(false)
    setMissedId(null)
  }

  const saveFaq = async () => {
    if (!draft.title.trim() || !draft.question.trim() || !draft.answer.trim()) return
    setSaving(true)
    let savedId = activeDoc?.id ?? null
    try {
      const similars = Array.from(new Set(draft.similars.map((s) => s.trim()).filter(Boolean)))
      if (missedId) {
        const res = await api.resolveMissed(missedId, draft.title.trim(), draft.question.trim(), draft.answer.trim(), draft.category, similars)
        savedId = res.doc_id
        toast.success('已转为 FAQ 并索引')
        setMissedId(null)
        setNav('faq')
      } else if (activeDoc && activeDoc.doc_type === 'faq') {
        const updated = await api.updateFaq(activeDoc.id, draft.title.trim(), draft.question.trim(), draft.answer.trim(), draft.category, similars)
        savedId = updated.id
        toast.success(updated.status === 'active' ? 'FAQ 已更新并重新索引' : 'FAQ 已保存，启用后才会进入检索')
      } else {
        const created = await api.createFaq(draft.title.trim(), draft.question.trim(), draft.answer.trim(), draft.category, similars)
        savedId = created.id
        toast.success('FAQ 已保存并索引')
        setNav('faq')
      }
      const list = await refresh()
      const doc = savedId ? list.find((d) => d.id === savedId) : undefined
      if (doc) await openDoc(doc)
    } catch (err: any) {
      toast.error(err?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const saveFile = async () => {
    if (!activeDoc || activeDoc.doc_type !== 'file') return
    setSaving(true)
    try {
      const updated = await api.updateDocMeta(activeDoc.id, {
        title: draft.title.trim(),
        category: draft.category.trim() || '未分类',
      })
      toast.success('文档信息已保存')
      await refresh()
      setSelectedId(updated.id)
    } catch (err: any) {
      toast.error(err?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const toggleStatus = async (doc: KnowledgeDoc) => {
    const next = doc.status === 'active' ? 'disabled' : 'active'
    try {
      await api.setDocStatus(doc.id, next)
      toast.success(next === 'active' ? '已启用并重新索引' : '已停用，已从检索移除')
      const list = await refresh()
      const current = list.find((d) => d.id === doc.id)
      if (current) {
        setChunks(await api.listChunks(doc.id).catch(() => []))
      }
    } catch (err: any) {
      toast.error(err?.message || '状态更新失败')
    }
  }

  const chosenIds = () => filtered.filter((doc) => selected.has(doc.id)).map((doc) => doc.id)

  const toggleSelected = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectFiltered = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      const allOn = filtered.length > 0 && filtered.every((doc) => next.has(doc.id))
      filtered.forEach((doc) => {
        if (allOn) next.delete(doc.id)
        else next.add(doc.id)
      })
      return next
    })
  }

  const removeDocs = async (ids: number[], title?: string) => {
    if (!ids.length) return
    const ok = await confirmDialog({
      title: ids.length === 1 ? `删除「${title || '这条知识'}」？` : `删除选中的 ${ids.length} 条？`,
      message: '只从当前环境的列表消失，AI 也不再引用。要生产一起关掉，把该行状态写成停用后导入。',
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    setActing(true)
    try {
      const result = await api.batchDocs(ids, 'delete')
      if (result.failed_ids.length) toast.error(`已删除 ${result.updated} 条，${result.failed_ids.length} 条失败`)
      else toast.success(`已删除 ${result.updated} 条`)
      if (selectedId != null && ids.includes(selectedId)) setSelectedId(null)
      setSelected((prev) => {
        const next = new Set(prev)
        ids.forEach((id) => next.delete(id))
        return next
      })
      await refresh()
    } catch (err: any) {
      toast.error(err?.message || '删除失败')
    } finally {
      setActing(false)
    }
  }

  const runBatch = async (action: 'active' | 'disabled') => {
    const ids = chosenIds()
    if (!ids.length) return
    setActing(true)
    try {
      const result = await api.batchDocs(ids, action)
      const label = action === 'active' ? '启用' : '停用'
      if (result.failed_ids.length) toast.error(`已${label} ${result.updated} 条，${result.failed_ids.length} 条失败`)
      else toast.success(`已${label} ${result.updated} 条`)
      setSelected(new Set())
      const list = await refresh()
      if (selectedId != null) {
        const current = list.find((doc) => doc.id === selectedId)
        if (current) setChunks(await api.listChunks(current.id).catch(() => []))
      }
    } catch (err: any) {
      toast.error(err?.message || '状态更新失败')
    } finally {
      setActing(false)
    }
  }

  const onExportFaq = async () => {
    setExporting(true)
    try {
      await downloadFaqPack()
    } catch (err: any) {
      toast.error(err?.message || '导出失败')
    } finally {
      setExporting(false)
    }
  }

  const onImportFaqPack = async (file: File) => {
    setPackImporting(true)
    try {
      const preview = await api.importFaqPack(file, false)
      const duplicates = preview.duplicates_overridden ?? []
      const unchanged = preview.created === 0
        && preview.content_updated === 0
        && preview.enabling === 0
        && preview.disabling_active === 0
        && preview.skipped_ambiguous.length === 0
        && duplicates.length === 0
      if (unchanged) {
        toast.info('这张表和当前知识库一致')
        return
      }
      const skip = preview.skipped_ambiguous.length
        ? `同标题有多条、未改动：${preview.skipped_ambiguous.slice(0, 5).join('、')}。`
        : ''
      const dup = duplicates.length
        ? `文件内重复标题已按最后一行处理：${duplicates.slice(0, 5).join('、')}。`
        : ''
      const ok = await confirmDialog({
        title: '按这张表更新知识库？',
        message: `新建 ${preview.created} 条，覆盖 ${preview.content_updated} 条，将启用 ${preview.enabling} 条，将停用 ${preview.disabling_active} 条当前已启用的。文件里没有的条目保持不变。${skip}${dup}`,
        confirmText: '导入',
        danger: preview.disabling_active > 0,
      })
      if (!ok) return
      const result = await api.importFaqPack(file, true)
      const resultDup = (result.duplicates_overridden ?? []).length
        ? `，重复标题按最后一行覆盖 ${(result.duplicates_overridden ?? []).slice(0, 5).join('、')}`
        : ''
      toast.success(`已导入：新建 ${result.created}，覆盖 ${result.content_updated}，启用 ${result.enabling}${resultDup}`)
      await refresh()
      setNav('faq')
      setCreating(false)
      setSelectedId(null)
    } catch (err: any) {
      toast.error(err?.message || '导入失败')
    } finally {
      setPackImporting(false)
    }
  }

  const onImportDialogs = async (file: File) => {
    setImporting(true)
    try {
      const result = await api.importDialogs(file)
      toast.success(`已导入 ${result.imported} 条，均为停用；另有 ${result.gap_count} 条待补未入库`)
      await refresh()
      setNav('faq')
      setCreating(false)
      setSelectedId(null)
    } catch (err: any) {
      toast.error(err?.message || '导入失败')
    } finally {
      setImporting(false)
    }
  }

  const onUpload = async (file: File) => {
    try {
      const doc = await api.uploadDoc(file)
      toast.success(`「${file.name}」已上传并索引`)
      await refresh()
      setNav('file')
      setSelectedId(doc.id)
      setCreating(false)
      setDraft({
        title: doc.title,
        category: doc.category || '未分类',
        question: '',
        answer: '',
        similars: [],
      })
      setChunksOpen(true)
      setChunks(await api.listChunks(doc.id).catch(() => []))
    } catch (err: any) {
      toast.error(err?.message || '上传失败')
    }
  }

  const runTest = async () => {
    if (!testQuery.trim()) return
    setTesting(true)
    try {
      setTestResult(await api.recallTest(testQuery.trim()))
    } finally {
      setTesting(false)
    }
  }

  const showEditor = creating || !!activeDoc
  const chosenCount = filtered.filter((doc) => selected.has(doc.id)).length

  const columns: ColumnsType<KnowledgeDoc> = [
    {
      title: '标题',
      dataIndex: 'title',
      render: (title: string, doc) => (
        <div className="min-w-0">
          <div className="truncate">{title}</div>
          {doc.doc_type === 'faq' && doc.question ? (
            <div className="text-[11px] text-gray-500 dark:text-gray-400 truncate">{doc.question}</div>
          ) : null}
        </div>
      ),
    },
    {
      title: '类型',
      dataIndex: 'doc_type',
      width: 72,
      render: (docType: string) => (
        <Tag color={docType === 'faq' ? 'cyan' : 'default'}>{docType === 'faq' ? 'FAQ' : '文档'}</Tag>
      ),
    },
    {
      title: '编号',
      dataIndex: 'id',
      width: 72,
      render: (id: number) => `#${id}`,
    },
    {
      title: '分类',
      dataIndex: 'category',
      width: 110,
      ellipsis: true,
      render: (value: string | undefined) => value || '未分类',
    },
    {
      title: '切片',
      dataIndex: 'chunk_count',
      width: 72,
      render: (count: number) => `${count} 切片`,
    },
    {
      title: '引用',
      dataIndex: 'hit_count',
      width: 88,
      render: (count: number | undefined) => `引用 ${count || 0} 次`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: (status: string) => (
        <Tag color={status === 'active' ? 'green' : 'default'}>{statusLabel(status)}</Tag>
      ),
    },
    {
      title: '操作',
      width: 80,
      render: (_, doc) => (
        <Button
          size="small"
          type={doc.status === 'active' ? 'default' : 'primary'}
          disabled={acting}
          onClick={(e) => {
            e.stopPropagation()
            toggleStatus(doc)
          }}
        >
          {doc.status === 'active' ? '停用' : '启用'}
        </Button>
      ),
    },
  ]

  const openRow = (doc: KnowledgeDoc, target: EventTarget | null) => {
    const node = target instanceof Element ? target : null
    if (node?.closest('.ant-table-selection-column, .ant-btn, button, a, .ant-switch, .ant-checkbox')) return
    openDoc(doc)
  }

  return (
    <div className="h-full flex flex-col bg-gray-50 dark:bg-gray-950">
      <div className="bg-white dark:bg-gray-900 border-b dark:border-gray-800 px-4 py-3 flex items-center gap-2 flex-wrap">
        <h1 className="text-base font-semibold text-gray-800 dark:text-gray-100">知识库</h1>
        <div className="flex-1" />
        <Space wrap size={8}>
          <Button type="primary" icon={<Plus size={14} />} onClick={() => startCreate()}>
            新建 FAQ
          </Button>
          <Button icon={<Download size={14} />} loading={exporting} onClick={onExportFaq}>
            {exporting ? '导出中…' : '导出 FAQ'}
          </Button>
          <FilePickButton
            accept=".xlsx"
            disabled={packImporting}
            icon={<UploadIcon size={14} />}
            label={packImporting ? '导入中…' : '导入 FAQ'}
            onFile={onImportFaqPack}
          />
          <FilePickButton
            accept=".xlsx"
            disabled={importing}
            icon={<UploadIcon size={14} />}
            label={importing ? '导入中…' : '导入历史对话'}
            onFile={onImportDialogs}
          />
          <FilePickButton
            accept=".pdf,.docx,.doc,.md,.txt"
            icon={<UploadIcon size={14} />}
            label="上传文档"
            onFile={onUpload}
          />
          <Button icon={<FlaskConical size={14} />} onClick={() => setTestOpen(true)}>
            召回测试
          </Button>
        </Space>
      </div>

      <div className="flex-1 flex min-h-0">
        <aside className="w-[200px] shrink-0 border-r dark:border-gray-800 bg-white dark:bg-gray-900 py-2 overflow-y-auto">
          <div className="px-2 flex flex-col gap-1">
            {NAV.map((item) => (
              <Button
                key={item.key}
                type="text"
                block
                htmlType="button"
                className={clsx(
                  '!justify-start !h-auto !py-2',
                  nav === item.key
                    ? '!bg-primary-50 !text-primary-700 dark:!bg-primary-600/15 dark:!text-primary-200'
                    : '!text-gray-600 dark:!text-gray-300',
                )}
                onClick={() => {
                  setNav(item.key)
                  if (item.key === 'file') setCategory('')
                  if (item.key === 'missed') {
                    setSelectedId(null)
                    setCreating(false)
                  }
                }}
              >
                <span className="flex w-full items-center">
                  <span className="flex-1 text-left">{item.label}</span>
                  <span className="text-[11px] opacity-70">{counts[item.key]}</span>
                </span>
              </Button>
            ))}
          </div>
          {nav !== 'missed' && nav !== 'file' && (
            <div className="mt-3 px-3">
              <div className="text-[11px] text-gray-400 mb-1">分类</div>
              <Select
                showSearch
                optionFilterProp="label"
                value={category}
                onChange={setCategory}
                style={{ width: '100%' }}
                options={[
                  { value: '', label: '全部分类' },
                  ...categories.map((name) => ({ value: name, label: name })),
                ]}
              />
            </div>
          )}
        </aside>

        <section className="flex-1 min-w-0 flex flex-col bg-gray-50 dark:bg-gray-950">
          {nav === 'missed' ? (
            <div className="flex-1 overflow-y-auto p-4">
              <div className="text-xs text-gray-400 mb-2">答不上来的问题会记在这里，补上答案后变成 FAQ。</div>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {missed.map((m) => (
                  <Card key={m.id} size="small">
                    <div className="flex items-center gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-gray-800 dark:text-gray-100 truncate">{m.question}</div>
                        <div className="text-[11px] text-orange-500 mt-0.5">被问 {m.count} 次{m.suggested_answer ? ' · 有待确认答案' : ''}</div>
                      </div>
                      {m.conversation_id != null && (
                        <Button
                          type="link"
                          size="small"
                          className="shrink-0"
                          onClick={() => navigate('/', { state: { conversationId: m.conversation_id } })}
                        >
                          打开会话
                        </Button>
                      )}
                      <Button
                        type="link"
                        size="small"
                        className="shrink-0"
                        onClick={() => startCreate({ title: m.question.slice(0, 30), question: m.question, answer: m.suggested_answer || '' }, m.id)}
                      >
                        补答案
                      </Button>
                    </div>
                  </Card>
                ))}
              </Space>
              {missed.length === 0 && <Empty title="暂无未命中问题" hint="AI 暂时都能从知识库里找到依据" />}
            </div>
          ) : (
            <>
              <div className="px-3 py-2">
                <Input.Search
                  allowClear
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="搜索标题、问法、答案"
                />
                <div className="mt-2 [&_.ant-tabs-content-holder]:hidden [&_.ant-tabs-nav]:!mb-0">
                  <Tabs
                    size="small"
                    activeKey={statusFilter}
                    onChange={(key) => setStatusFilter(key as 'all' | 'active' | 'disabled')}
                    tabBarExtraContent={
                      <Button
                        type="link"
                        size="small"
                        disabled={!filtered.length || acting}
                        onClick={toggleSelectFiltered}
                      >
                        {filtered.length > 0 && filtered.every((doc) => selected.has(doc.id)) ? '取消全选' : '全选当前筛选'}
                      </Button>
                    }
                    items={[
                      { key: 'all', label: '全部' },
                      { key: 'active', label: '启用' },
                      { key: 'disabled', label: '停用' },
                    ]}
                  />
                </div>
                {chosenCount > 0 && (
                  <Space className="mt-2" size={8}>
                    <span className="text-xs text-gray-500">已选 {chosenCount}</span>
                    <Button size="small" type="primary" disabled={acting} onClick={() => runBatch('active')}>启用</Button>
                    <Button size="small" disabled={acting} onClick={() => runBatch('disabled')}>停用</Button>
                    <Button
                      size="small"
                      danger
                      disabled={acting}
                      onClick={() => {
                        const rows = filtered.filter((doc) => selected.has(doc.id))
                        removeDocs(rows.map((doc) => doc.id), rows.length === 1 ? rows[0].title : undefined)
                      }}
                    >
                      删除
                    </Button>
                  </Space>
                )}
              </div>
              <div className="flex-1 min-h-0 overflow-auto px-3 pb-3">
                <Table
                  size="small"
                  rowKey="id"
                  columns={columns}
                  dataSource={shown}
                  pagination={false}
                  scroll={{ x: 860 }}
                  rowSelection={{
                    selectedRowKeys: shown.filter((doc) => selected.has(doc.id)).map((doc) => doc.id),
                    hideSelectAll: true,
                    onSelect: (doc) => toggleSelected(doc.id),
                  }}
                  onRow={(doc) => ({
                    onClick: (event) => openRow(doc, event.target),
                    onKeyDown: (event) => {
                      if (event.key === 'Enter') openRow(doc, event.target)
                    },
                    tabIndex: 0,
                    style: { cursor: 'pointer' },
                  })}
                  rowClassName={(doc) =>
                    clsx(selectedId === doc.id && !creating && '[&>td]:!bg-primary-50 dark:[&>td]:!bg-primary-600/15')
                  }
                  footer={
                    filtered.length > shown.length
                      ? () => (
                          <Space size={12}>
                            <span className="text-xs text-gray-400">已显示 {shown.length} / {filtered.length} 条</span>
                            <Button type="link" size="small" onClick={() => setLimit((n) => n + 200)}>
                              显示更多
                            </Button>
                          </Space>
                        )
                      : undefined
                  }
                  locale={{
                    emptyText: <Empty icon={FileText} title="没有匹配的知识" hint="新建 FAQ，或上传 PDF / Word / Markdown" />,
                  }}
                />
              </div>
            </>
          )}
        </section>

        <aside className="w-[360px] shrink-0 border-l dark:border-gray-800 bg-white dark:bg-gray-900 flex flex-col">
          {!showEditor && (
            <div className="h-full flex items-center justify-center text-sm text-gray-400 px-6 text-center">
              选择一条知识，或新建 FAQ
            </div>
          )}
          {showEditor && (creating || activeDoc?.doc_type === 'faq') && (
            <Form layout="vertical" requiredMark={false} onFinish={saveFaq} className="flex-1 min-h-0 flex flex-col">
              <div className="p-4 pb-0 flex items-center gap-2">
                <div className="text-sm font-medium text-gray-800 dark:text-gray-100 flex-1">
                  {missedId ? '未命中转 FAQ' : creating ? '新建 FAQ' : '编辑 FAQ'}
                </div>
                <Button type="text" size="small" htmlType="button" icon={<X size={16} />} aria-label="关闭" onClick={closeEditor} />
              </div>
              <div className="flex-1 overflow-y-auto px-4">
                <Form.Item label="标题" style={{ marginBottom: 12 }}>
                  <Input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} placeholder="产品价格" />
                </Form.Item>
                <Form.Item label="分类" style={{ marginBottom: 12 }}>
                  <CategoryField
                    value={draft.category}
                    categories={categories}
                    placeholder="未分类"
                    onChange={(value) => setDraft({ ...draft, category: value })}
                  />
                </Form.Item>
                <Form.Item label="标准问" style={{ marginBottom: 12 }}>
                  <Input value={draft.question} onChange={(e) => setDraft({ ...draft, question: e.target.value })} placeholder="这个多少钱" />
                </Form.Item>
                <Form.Item label="相似问" style={{ marginBottom: 12 }}>
                  <Input.TextArea
                    value={draft.similars.join('\n')}
                    onChange={(e) => setDraft({ ...draft, similars: e.target.value.split('\n') })}
                    onPaste={(e) => {
                      const text = e.clipboardData.getData('text')
                      if (!text) return
                      const parts = text.split(/[\n,，、/／]+/).map((s) => s.trim()).filter(Boolean)
                      const merged = Array.from(new Set([...draft.similars.filter((s) => s.trim()), ...parts]))
                      e.preventDefault()
                      setDraft({ ...draft, similars: merged })
                    }}
                    rows={5}
                    placeholder={'每行一条相似问\n可整段粘贴，自动按 换行/逗号/顿号/斜杠 拆分'}
                  />
                </Form.Item>
                <Form.Item label="答案" style={{ marginBottom: 12 }}>
                  <Input.TextArea value={draft.answer} onChange={(e) => setDraft({ ...draft, answer: e.target.value })} rows={6} placeholder="标准款 99 元" />
                </Form.Item>
                {activeDoc && !creating && (
                  <EnableRow doc={activeDoc} onToggle={() => toggleStatus(activeDoc)} />
                )}
                <ChunkList chunks={chunks} open={chunksOpen} onToggle={() => setChunksOpen(!chunksOpen)} />
              </div>
              <div className="p-4 border-t dark:border-gray-800">
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <Button type="primary" htmlType="submit" block loading={saving}>
                    {saving ? '保存中…' : '保存并索引'}
                  </Button>
                  {activeDoc && !creating && (
                    <Button
                      htmlType="button"
                      type="text"
                      danger
                      block
                      disabled={acting}
                      onClick={() => removeDocs([activeDoc.id], activeDoc.title)}
                    >
                      删除
                    </Button>
                  )}
                </Space>
              </div>
            </Form>
          )}
          {showEditor && activeDoc && activeDoc.doc_type === 'file' && !creating && (
            <Form component="div" layout="vertical" requiredMark={false} className="flex-1 min-h-0 flex flex-col">
              <div className="p-4 pb-0 flex items-center gap-2">
                <div className="text-sm font-medium text-gray-800 dark:text-gray-100 flex-1">文档</div>
                <Button type="text" size="small" icon={<X size={16} />} aria-label="关闭" onClick={closeEditor} />
              </div>
              <div className="flex-1 overflow-y-auto px-4">
                <Form.Item label="标题" style={{ marginBottom: 12 }}>
                  <Input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
                </Form.Item>
                <Form.Item label="分类" style={{ marginBottom: 12 }}>
                  <CategoryField
                    value={draft.category}
                    categories={categories}
                    onChange={(value) => setDraft({ ...draft, category: value })}
                  />
                </Form.Item>
                <EnableRow doc={activeDoc} onToggle={() => toggleStatus(activeDoc)} />
                <ChunkList chunks={chunks} open={chunksOpen} onToggle={() => setChunksOpen(!chunksOpen)} />
              </div>
              <div className="p-4 border-t dark:border-gray-800">
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <Button type="primary" block loading={saving} onClick={saveFile}>
                    {saving ? '保存中…' : '保存'}
                  </Button>
                  <Button
                    type="text"
                    danger
                    block
                    disabled={acting}
                    onClick={() => removeDocs([activeDoc.id], activeDoc.title)}
                  >
                    删除
                  </Button>
                </Space>
              </div>
            </Form>
          )}
        </aside>
      </div>

      <Modal
        title="召回测试"
        open={testOpen}
        onCancel={() => setTestOpen(false)}
        onOk={runTest}
        okText={testing ? '检索中' : '测试'}
        cancelText="关闭"
        confirmLoading={testing}
        width={480}
      >
        <Input
          value={testQuery}
          onChange={(e) => setTestQuery(e.target.value)}
          onPressEnter={runTest}
          placeholder="输入测试问题"
        />
        {testResult && (
          <div className="mt-4 space-y-3 text-sm">
            {testResult.degraded && (
              <div className="text-xs bg-yellow-50 text-yellow-700 rounded-lg px-3 py-2">当前为纯 BM25，未配置 Embedding</div>
            )}
            <div>
              <span className="text-gray-400 text-xs">改写后查询</span>
              <div className="mt-1">
                <Space size={4} wrap>
                  {testResult.rewritten_queries.map((item, i) => (
                    <Tag key={i}>{item}</Tag>
                  ))}
                </Space>
              </div>
            </div>
            <div className="text-xs text-gray-500">
              最高分 {testResult.rerank_top_score?.toFixed(3) ?? '未启用 rerank'} · 阈值 {testResult.threshold}
              <span className={clsx('ml-2', testResult.passed ? 'text-primary-600' : 'text-red-500')}>
                {testResult.passed ? '通过' : '未过阈值'}
              </span>
            </div>
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {testResult.hits.map((hit, i) => (
                <Card key={i} size="small">
                  <div className="text-[11px] text-gray-400 mb-1">{hit.source}{hit.score != null ? ` · ${hit.score.toFixed(3)}` : ''}</div>
                  <div className="text-gray-700 dark:text-gray-200 whitespace-pre-wrap text-xs">{hit.content}</div>
                </Card>
              ))}
            </Space>
            {testResult.hits.length === 0 && <div className="text-xs text-gray-400">无召回结果</div>}
          </div>
        )}
      </Modal>
    </div>
  )
}

function FilePickButton({
  accept,
  disabled,
  icon,
  label,
  onFile,
}: {
  accept: string
  disabled?: boolean
  icon: ReactNode
  label: string
  onFile: (file: File) => void
}) {
  return (
    <Upload
      accept={accept}
      disabled={disabled}
      showUploadList={false}
      maxCount={1}
      beforeUpload={(file) => {
        onFile(file)
        return Upload.LIST_IGNORE
      }}
    >
      <Button icon={icon} disabled={disabled} loading={disabled}>
        {label}
      </Button>
    </Upload>
  )
}

function CategoryField({
  value,
  categories,
  placeholder,
  onChange,
}: {
  value: string
  categories: string[]
  placeholder?: string
  onChange: (value: string) => void
}) {
  return (
    <AutoComplete
      value={value}
      style={{ width: '100%' }}
      placeholder={placeholder}
      options={categories.map((name) => ({ value: name }))}
      filterOption={(input, option) => String(option?.value ?? '').toLowerCase().includes(input.toLowerCase())}
      onChange={onChange}
    />
  )
}

function EnableRow({ doc, onToggle }: { doc: KnowledgeDoc; onToggle: () => void }) {
  const on = doc.status === 'active'
  return (
    <div className="flex items-center justify-between text-sm mb-3">
      <span className="text-gray-500">{on ? '检索中' : '已停用，不参与回答'}</span>
      <Switch checked={on} onChange={onToggle} />
    </div>
  )
}

function ChunkList({ chunks, open, onToggle }: { chunks: KnowledgeChunk[]; open: boolean; onToggle: () => void }) {
  return (
    <div className="mb-3">
      <Button type="link" size="small" style={{ padding: 0 }} onClick={onToggle}>
        切片 {chunks.length} {open ? '收起' : '展开'}
      </Button>
      {open && (
        <div className="mt-2 space-y-2">
          {chunks.length === 0 && <div className="text-xs text-gray-400">停用或尚未索引时没有切片</div>}
          {chunks.map((chunk) => (
            <div key={chunk.id} className="text-xs bg-gray-50 dark:bg-gray-800 rounded-lg p-2 whitespace-pre-wrap text-gray-600 dark:text-gray-300">
              {chunk.content}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
