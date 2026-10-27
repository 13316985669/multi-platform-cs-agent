import { Card, Statistic } from 'antd'
import {
  Activity,
  Coins,
  Cpu,
  Flag,
  MessageSquare,
  PieChart as PieIcon,
  Timer,
  UserCheck,
  Users,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, StatsOverview, TokenUsagePoint } from '../api/client'
import Skeleton from '../components/ui/Skeleton'

interface TrendPoint {
  date: string
  count: number
}

const PLATFORM_LABEL: Record<string, string> = {
  douyin: '抖音',
  xiaohongshu: '小红书',
  mock: 'Mock',
}

const PLATFORM_COLOR: Record<string, string> = {
  douyin: '#161823',
  xiaohongshu: '#ff2442',
  mock: '#9ca3af',
}

export default function Dashboard() {
  const [overview, setOverview] = useState<StatsOverview | null>(null)
  const [trend, setTrend] = useState<TrendPoint[]>([])
  const [tokenUsage, setTokenUsage] = useState<TokenUsagePoint[]>([])
  const [missedTop, setMissedTop] = useState<{ question: string; count: number }[]>([])

  useEffect(() => {
    api.statsOverview().then(setOverview)
    api.statsTrend(7).then(setTrend)
    api.tokenUsage(7).then(setTokenUsage)
    api.missedTop().then(setMissedTop).catch(() => setMissedTop([]))
  }, [])

  if (!overview) {
    return (
      <div className="h-full overflow-y-auto bg-gray-50 dark:bg-gray-950 p-6">
        <Skeleton rows={4} className="max-w-5xl mx-auto" />
      </div>
    )
  }

  const cards = [
    { label: '今日会话', value: overview.today_conversations, icon: MessageSquare, color: 'text-blue-600 bg-blue-50' },
    { label: '今日消息', value: overview.today_messages, icon: Activity, color: 'text-indigo-600 bg-indigo-50' },
    { label: 'AI 应答比', value: `${Math.round(overview.ai_reply_rate * 100)}%`, icon: Cpu, color: 'text-green-600 bg-green-50', hint: 'AI 消息数 ÷ 用户消息数' },
    { label: '转人工率', value: `${Math.round(overview.handoff_rate * 100)}%`, icon: UserCheck, color: 'text-orange-600 bg-orange-50' },
    { label: '平均首次响应', value: overview.avg_first_response_seconds > 0 ? `${Math.round(overview.avg_first_response_seconds)}s` : '-', icon: Timer, color: 'text-sky-600 bg-sky-50' },
    { label: '3分钟回复率', value: `${Math.round(overview.reply_within_3min_rate * 100)}%`, icon: Timer, color: 'text-cyan-600 bg-cyan-50', hint: '平台考核 ≥70%' },
    { label: '今日 Token', value: overview.today_tokens.toLocaleString(), icon: Coins, color: 'text-violet-600 bg-violet-50', hint: `≈ ¥${overview.today_token_cost_yuan}` },
    { label: '今日评论', value: overview.today_comments, icon: MessageSquare, color: 'text-pink-600 bg-pink-50', hint: '内容矩阵采集' },
    { label: '今日留资', value: overview.today_leads, icon: UserCheck, color: 'text-amber-600 bg-amber-50', hint: '漏斗 lead 层' },
    { label: '今日加企微', value: overview.today_wecom_adds, icon: Users, color: 'text-emerald-600 bg-emerald-50', hint: '漏斗 wecom 层' },
    { label: '在线客服', value: overview.active_agents, icon: Users, color: 'text-teal-600 bg-teal-50' },
    { label: '待人工', value: overview.pending_conversations, icon: Timer, color: 'text-red-600 bg-red-50' },
    { label: '7日检索未通过', value: `${Math.round((overview.retrieval_miss_rate_7d || 0) * 100)}%`, icon: Cpu, color: 'text-orange-600 bg-orange-50' },
    { label: '7日不佳', value: overview.bad_case_count_7d || 0, icon: Flag, color: 'text-red-600 bg-red-50' },
    { label: '7日发送失败', value: overview.send_failed_count_7d || 0, icon: Activity, color: 'text-rose-600 bg-rose-50' },
  ]

  const platformTotal = Object.values(overview.platform_breakdown).reduce((a, b) => a + b, 0)
  const handoffReasons = Object.entries(overview.handoff_reasons_7d || {})

  return (
    <div className="h-full overflow-y-auto bg-gray-50 dark:bg-gray-950 p-6">
      <div className="max-w-5xl mx-auto">
        <h1 className="text-lg font-semibold text-gray-800 dark:text-gray-100 mb-4">数据看板</h1>
        <div className="grid grid-cols-4 gap-3 mb-5">
          {cards.map((card) => (
            <Card key={card.label} size="small">
              <Statistic title={card.label} value={card.value} />
              {card.hint ? <div className="text-xs text-gray-400 mt-1">{card.hint}</div> : null}
            </Card>
          ))}
        </div>

        {handoffReasons.length > 0 && (
          <div className="text-xs text-gray-500 dark:text-gray-400 mb-4">
            近 7 日转人工：{handoffReasons.map(([reason, count]) => `${reason} ${count}`).join(' · ')}
          </div>
        )}

        <div className="grid grid-cols-3 gap-4 mb-4">
          {/* 会话趋势面积图 */}
          <Card className="col-span-2" title="近 7 天会话量">
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={trend} margin={{ top: 4, right: 8, left: -18, bottom: 0 }}>
                <defs>
                  <linearGradient id="convFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.25} />
                    <stop offset="100%" stopColor="#3b82f6" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#94a3b8' }} />
                <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} allowDecimals={false} />
                <Tooltip />
                <Area type="monotone" dataKey="count" name="会话数" stroke="#3b82f6" strokeWidth={2} fill="url(#convFill)" />
              </AreaChart>
            </ResponsiveContainer>
          </Card>

          {/* 平台分布 */}
          <Card title="今日平台分布">
            {platformTotal === 0 ? (
              <div className="text-xs text-gray-300 dark:text-gray-600 text-center py-10">今日暂无会话</div>
            ) : (
              <div className="space-y-3 pt-2">
                {Object.entries(overview.platform_breakdown).map(([platform, count]) => {
                  const pct = Math.round((count / platformTotal) * 100)
                  return (
                    <div key={platform}>
                      <div className="flex justify-between text-xs mb-1">
                        <span className="text-gray-600 dark:text-gray-300">{PLATFORM_LABEL[platform] || platform}</span>
                        <span className="text-gray-400 dark:text-gray-500">
                          {count} 个 · {pct}%
                        </span>
                      </div>
                      <div className="h-2 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${pct}%`,
                            background: PLATFORM_COLOR[platform] || '#3b82f6',
                          }}
                        />
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </Card>
        </div>

        <Card className="mt-4" title="近 7 天 Token 用量">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={tokenUsage} margin={{ top: 4, right: 8, left: -12, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: '#94a3b8' }} />
              <YAxis tick={{ fontSize: 11, fill: '#94a3b8' }} />
              <Tooltip
                formatter={(value) => [Number(value).toLocaleString(), 'Token']}
              />
              <Bar dataKey="total_tokens" name="Token" fill="#8b5cf6" radius={[4, 4, 0, 0]} maxBarSize={36} />
            </BarChart>
          </ResponsiveContainer>
        </Card>

        <Card className="mt-4" title="未命中问题 TOP">
          {missedTop.length === 0 ? (
            <div className="text-xs text-gray-300 dark:text-gray-600 text-center py-6">暂无未命中沉淀</div>
          ) : (
            <div className="space-y-2">
              {missedTop.map((m) => (
                <div key={m.question} className="flex items-center justify-between text-sm">
                  <span className="text-gray-700 dark:text-gray-200 truncate pr-3">{m.question}</span>
                  <span className="text-xs text-orange-500 shrink-0">被问 {m.count} 次</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
