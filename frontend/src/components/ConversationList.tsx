import { Button, Dropdown, Input, Tabs, Tag, Typography } from 'antd'
import clsx from 'clsx'
import { Filter, Search } from 'lucide-react'
import { useState } from 'react'
import { Conversation } from '../api/client'
import { useAuth } from '../store'

export type InboxTab = 'mine' | 'pending' | 'active' | 'closed'

const TABS: { key: InboxTab; label: string }[] = [
  { key: 'mine', label: '我的对话' },
  { key: 'pending', label: '排队' },
  { key: 'active', label: '同事' },
  { key: 'closed', label: '已结束' },
]

const PLATFORM_FILTERS = [
  { value: '', label: '全部平台' },
  { value: 'douyin', label: '抖音' },
  { value: 'xiaohongshu', label: '小红书' },
  { value: 'mock', label: '模拟' },
]

const PLATFORM_LABEL: Record<string, string> = {
  douyin: '抖音',
  xiaohongshu: '小红书',
  mock: '模拟',
}

const PLATFORM_COLOR: Record<string, string> = {
  douyin: '#161823',
  xiaohongshu: '#ff2442',
  mock: '#00b8a9',
}

function waitLabel(seconds: number) {
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m`
}

export default function ConversationList({
  conversations,
  currentId,
  onSelect,
  tab,
  onTabChange,
  platform,
  onPlatformChange,
  counts,
  width,
}: {
  conversations: Conversation[]
  currentId: number | null
  onSelect: (id: number) => void
  tab: InboxTab
  onTabChange: (t: InboxTab) => void
  platform: string
  onPlatformChange: (p: string) => void
  counts: Record<string, number>
  width: number
}) {
  const { agent } = useAuth()
  const [q, setQ] = useState('')

  const filtered = conversations.filter((c) => {
    if (!q) return true
    const hay = `${c.customer.nickname} ${c.last_message?.content || ''} ${c.summary}`
    return hay.toLowerCase().includes(q.toLowerCase())
  })

  return (
    <div className="border-r dark:border-gray-800 flex flex-col bg-white dark:bg-gray-900 shrink-0" style={{ width }}>
      <Tabs
        size="small"
        activeKey={tab}
        onChange={(key) => onTabChange(key as InboxTab)}
        tabBarStyle={{ marginBottom: 0 }}
        className="[&_.ant-tabs-content-holder]:hidden [&_.ant-tabs-nav-list]:w-full [&_.ant-tabs-tab]:flex-1 [&_.ant-tabs-tab]:justify-center"
        items={TABS.map((t) => ({
          key: t.key,
          label: (
            <span>
              {t.label}
              {(counts[t.key] || 0) > 0 && (
                <span className={clsx('ml-0.5 text-[10px]', t.key === 'pending' ? 'text-orange-500' : 'text-gray-400')}>
                  {counts[t.key]}
                </span>
              )}
            </span>
          ),
        }))}
      />

      <div className="px-2.5 py-2 flex items-center gap-1.5">
        <Input
          allowClear
          size="small"
          className="flex-1"
          prefix={<Search size={13} />}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索对话"
        />
        <Dropdown
          trigger={['click']}
          menu={{
            items: PLATFORM_FILTERS.map((p) => ({
              key: p.value || 'all',
              label: p.label,
            })),
            selectable: true,
            selectedKeys: [platform || 'all'],
            onClick: ({ key }) => onPlatformChange(key === 'all' ? '' : String(key)),
          }}
        >
          <Button
            size="small"
            type={platform ? 'primary' : 'default'}
            icon={<Filter size={14} />}
            title="平台筛选"
            aria-label="平台筛选"
          />
        </Dropdown>
      </div>

      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 && (
          <Typography.Text type="secondary" className="block text-center text-xs py-10">
            暂无会话
          </Typography.Text>
        )}
        {filtered.map((c) => {
          const selected = c.id === currentId
          const mine = agent && c.assignee_id === agent.id
          const wait = c.awaiting_first_response && (c.wait_seconds || 0) > 0
          const closed = c.status === 'closed'
          const platformColor = PLATFORM_COLOR[c.platform] || '#00b8a9'
          const tags = c.customer.tags || []
          return (
            <Button
              key={c.id}
              type="text"
              block
              onClick={() => onSelect(c.id)}
              className={clsx(
                '!h-auto !justify-start !whitespace-normal !rounded-none !px-3 !py-2 !border-x-0 !border-t-0 !border-b !border-solid !border-gray-100 dark:!border-gray-800 relative',
                selected
                  ? '!bg-primary-50 dark:!bg-primary-600/15'
                  : 'hover:!bg-gray-50 dark:hover:!bg-gray-800/60',
                c.overdue && !selected && '!bg-red-50/70 dark:!bg-red-950/20',
              )}
            >
              <div className="relative flex items-start gap-2 w-full text-left">
              {selected && (
                <span className="absolute -left-3 -top-2 -bottom-2 w-[3px] bg-primary-600" />
              )}
                <div className="relative shrink-0">
                  {c.customer.avatar ? (
                    <img
                      src={c.customer.avatar}
                      alt=""
                      className={clsx(
                        'w-9 h-9 rounded-full object-cover',
                        closed && 'grayscale opacity-70',
                      )}
                    />
                  ) : (
                    <div
                      className={clsx(
                        'w-9 h-9 rounded-full flex items-center justify-center text-white text-sm font-medium',
                        closed && 'grayscale opacity-70',
                      )}
                      style={{ background: platformColor }}
                    >
                      {c.customer.nickname?.slice(0, 1) || '?'}
                    </div>
                  )}
                  {c.unread_count > 0 && (
                    <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-0.5 bg-red-500 text-white text-[9px] rounded-full flex items-center justify-center leading-none">
                      {c.unread_count > 99 ? '99+' : c.unread_count}
                    </span>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1">
                    <span className="text-[13px] font-medium text-gray-800 dark:text-gray-100 truncate">
                      {c.customer.nickname}
                    </span>
                    {c.is_returning && (
                      <Tag color="gold" className="!mr-0 !text-[9px] !leading-none !px-1 !py-px">
                        回头客
                      </Tag>
                    )}
                    {c.transferred_in && (
                      <Tag color="cyan" className="!mr-0 !text-[9px] !leading-none !px-1 !py-px">
                        转入
                      </Tag>
                    )}
                    <span className="ml-auto text-[10px] text-gray-400 shrink-0">
                      {new Date(c.last_message_at).toLocaleTimeString('zh-CN', {
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                  </div>
                  <div className="flex items-center gap-1 mt-0.5">
                    <span className="text-[10px] text-gray-400">{PLATFORM_LABEL[c.platform]}</span>
                    {c.mode === 'ai' && (
                      <span className="text-[10px] text-primary-600">AI</span>
                    )}
                    {c.mode === 'pending' && (
                      <Tag color="orange" className="!mr-0 !text-[10px] !leading-none !px-1">
                        排队
                      </Tag>
                    )}
                    {c.assignee_name && c.mode === 'human' && !mine && (
                      <span className="text-[10px] text-gray-400 truncate">{c.assignee_name}</span>
                    )}
                    {wait && (
                      <span
                        className={clsx(
                          'ml-auto text-[10px] tabular-nums',
                          c.overdue ? 'text-red-500 font-medium' : 'text-gray-400',
                        )}
                      >
                        {waitLabel(c.wait_seconds || 0)}
                      </span>
                    )}
                  </div>
                  <div className="text-[12px] text-gray-500 dark:text-gray-400 truncate mt-0.5">
                    {c.last_message?.content || '暂无消息'}
                  </div>
                  {tags.length > 0 && (
                    <div className="flex gap-1 mt-1 overflow-hidden [mask-image:linear-gradient(to_right,black_70%,transparent)]">
                      {tags.slice(0, 3).map((t) => (
                        <Tag key={t} className="!mr-0 !text-[9px] !leading-none !px-1 !py-px">
                          {t}
                        </Tag>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </Button>
          )
        })}
      </div>
    </div>
  )
}
