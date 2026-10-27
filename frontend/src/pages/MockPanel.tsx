import { Alert, Button, Card, Input, Space, Typography } from 'antd'
import { FormEvent, useEffect, useState } from 'react'
import { api, type Message } from '../api/client'
import { toast } from '../components/ui/toast'

const PLATFORMS = [
  { key: 'douyin', label: '抖音', className: 'bg-gray-900' },
  { key: 'xiaohongshu', label: '小红书', className: 'bg-xhs' },
] as const

const PRESET_MESSAGES = [
  '在吗，这个多少钱',
  '质量怎么样啊',
  '你们支持开发票吗',
  '我要找真人客服',
  '这东西太差了，我要投诉！',
]

const REASON_LABEL: Record<string, string> = {
  passed: '检索通过',
  no_hits: '两路都没召回',
  rerank_below_threshold: '精排分低于阈值',
  rerank_unavailable: '精排未用上',
  dense_gap: '向量分差不够',
  bm25_gap: '关键词分差不够',
}

type Round = {
  id: number
  userText: string
  reason: string
  score: string
  confidence: string
  reply: string
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function describeRetrieval(message: Message): { reason: string; score: string } {
  const retrieval = message.extra?.retrieval
  if (!retrieval) return { reason: '还没有检索记录', score: '—' }
  let reason = REASON_LABEL[retrieval.reason || ''] || retrieval.reason || '未知'
  if (retrieval.reason === 'passed' && retrieval.rerank_status === 'unavailable') {
    reason = '检索通过，精排未用上'
  }
  const score = retrieval.rerank_top_score == null ? '无精排分' : String(retrieval.rerank_top_score)
  return { reason, score }
}

function roundsFrom(messages: Message[]): Round[] {
  const rounds: Round[] = []
  for (let i = 0; i < messages.length; i++) {
    const message = messages[i]
    if (message.sender_type !== 'user' || message.is_internal) continue
    const replies: string[] = []
    for (let j = i + 1; j < messages.length; j++) {
      const next = messages[j]
      if (next.sender_type === 'user') break
      if (next.is_internal) continue
      if (next.sender_type === 'ai' || next.sender_type === 'system') replies.push(next.content)
    }
    const described = describeRetrieval(message)
    const confidence = messages.slice(i + 1).find((item) => item.sender_type === 'ai' && !item.is_internal)
      ?.extra?.answer_confidence
    const confidenceText = confidence
      ? `忠实度 ${confidence.faithfulness ?? '—'} · 上下文精度 ${confidence.context_precision ?? '—'} · 综合 ${confidence.score ?? '—'}`
      : ''
    rounds.push({
      id: message.id,
      userText: message.content,
      reason: described.reason,
      score: described.score,
      confidence: confidenceText,
      reply: replies.join('\n'),
    })
  }
  return rounds.reverse()
}

async function loadRounds(platform: string, userId: string): Promise<Round[]> {
  const [active, pending] = await Promise.all([
    api.listConversations('active', platform),
    api.listConversations('pending', platform),
  ])
  const matched = [...active, ...pending]
    .filter((item) => item.customer.platform_user_id === userId)
    .sort((a, b) => (a.last_message_at < b.last_message_at ? 1 : -1))
  const conversation = matched[0]
  if (!conversation) return []
  const messages = await api.listMessages(conversation.id)
  return roundsFrom(messages)
}

export default function MockPanel() {
  const [platform, setPlatform] = useState<string>('douyin')
  const [userId, setUserId] = useState('user_001')
  const [nickname, setNickname] = useState('测试用户')
  const [content, setContent] = useState('')
  const [rounds, setRounds] = useState<Round[]>([])
  const [waiting, setWaiting] = useState(false)

  useEffect(() => {
    let cancelled = false
    loadRounds(platform, userId)
      .then((items) => {
        if (!cancelled) setRounds(items)
      })
      .catch(() => {
        if (!cancelled) setRounds([])
      })
    return () => {
      cancelled = true
    }
  }, [platform, userId])

  const send = async (e?: FormEvent) => {
    e?.preventDefault()
    const text = content.trim()
    if (!text || waiting) return
    setWaiting(true)
    try {
      await api.mockIncoming(platform, userId, nickname, text)
      setContent('')
      const deadline = Date.now() + 20000
      while (Date.now() < deadline) {
        const items = await loadRounds(platform, userId)
        setRounds(items)
        const hit = items.find((item) => item.userText === text)
        if (hit?.reply) break
        await sleep(800)
      }
    } catch (err: any) {
      toast.error(err?.message || '发送失败')
    } finally {
      setWaiting(false)
    }
  }

  return (
    <div className="h-full overflow-auto p-6">
      <Alert className="max-w-2xl mx-auto !mb-4" type="warning" showIcon message="演示模式：模拟用户发私信。本页会记下原话、检索结论和回复，刷新后仍从会话读取。" />
      <div className="max-w-2xl mx-auto space-y-4">
        <Card title="扮演用户">
          <Space className="mb-3">
            {PLATFORMS.map((p) => (
              <Button key={p.key} type={platform === p.key ? 'primary' : 'default'} onClick={() => setPlatform(p.key)}>
                {p.label}
              </Button>
            ))}
          </Space>
          <Space className="mb-3" style={{ width: '100%' }}>
            <Input style={{ width: 160 }} value={userId} onChange={(e) => setUserId(e.target.value)} placeholder="用户 ID" />
            <Input value={nickname} onChange={(e) => setNickname(e.target.value)} placeholder="昵称" />
          </Space>
          <form onSubmit={send}>
            <Space.Compact style={{ width: '100%' }}>
              <Input value={content} onChange={(e) => setContent(e.target.value)} placeholder="输入用户消息..." />
              <Button type="primary" htmlType="submit" loading={waiting}>发送</Button>
            </Space.Compact>
          </form>
          <Space wrap className="mt-3">
            {PRESET_MESSAGES.map((m) => (
              <Button key={m} size="small" onClick={() => setContent(m)}>{m}</Button>
            ))}
          </Space>
        </Card>
        <Card title="对话记录">
          <Space direction="vertical" style={{ width: '100%' }}>
            {rounds.map((round) => (
              <Card key={round.id} size="small">
                <div>{round.userText}</div>
                <Typography.Text type="secondary">
                  {round.reason} · 精排分 {round.score}
                  {round.confidence ? ` · ${round.confidence}` : ''}
                </Typography.Text>
                <div className="whitespace-pre-wrap">{round.reply || '等待回复'}</div>
              </Card>
            ))}
            {rounds.length === 0 && <Typography.Text type="secondary">暂无记录</Typography.Text>}
          </Space>
        </Card>
      </div>
    </div>
  )
}
