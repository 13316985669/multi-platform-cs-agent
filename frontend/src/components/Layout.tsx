import { Badge, Button, Dropdown, Layout as AntLayout, Menu, theme as antdTheme } from 'antd'
import {
  BarChart3,
  BookOpen,
  CalendarClock,
  ClipboardList,
  Filter,
  FlaskConical,
  Lightbulb,
  LogOut,
  MessageCircle,
  MessageSquare,
  Moon,
  PenSquare,
  Settings as SettingsIcon,
  Sparkles,
  Sun,
  TrendingUp,
  UserCheck,
  Users,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { useTheme } from '../hooks/useTheme'
import { useAuth } from '../store'
import { canSee, firstVisiblePath, moduleForPath } from '../utils/modules'

const { Sider, Content } = AntLayout

const CS_NAV = [
  { path: '/', label: '对话', icon: MessageSquare, module: 'workbench' },
  { path: '/knowledge', label: '知识库', icon: BookOpen, module: 'knowledge' },
  { path: '/tickets', label: '工单', icon: ClipboardList, module: 'tickets' },
  { path: '/leads', label: '留资', icon: UserCheck, module: 'leads' },
  { path: '/dashboard', label: '数据', icon: BarChart3, module: 'dashboard' },
]

const MATRIX_NAV = [
  { path: '/accounts', label: '账号', icon: Users, module: 'accounts' },
  { path: '/studio', label: '创作', icon: PenSquare, module: 'studio' },
  { path: '/publish', label: '发布', icon: CalendarClock, module: 'publish' },
  { path: '/comments', label: '评论', icon: MessageCircle, module: 'comments' },
  { path: '/funnel', label: '漏斗', icon: Filter, module: 'funnel' },
  { path: '/analytics', label: '效果', icon: TrendingUp, module: 'analytics' },
  { path: '/inspiration', label: '灵感', icon: Lightbulb, module: 'inspiration' },
  { path: '/mock', label: '模拟', icon: FlaskConical, module: 'mock' },
]

const STATUS_MAP: Record<string, { label: string; color: string }> = {
  active: { label: '接待中', color: '#22c55e' },
  resting: { label: '休息中', color: '#eab308' },
  offline: { label: '离线', color: '#9ca3af' },
}

function menuItems(
  items: { path: string; label: string; icon: typeof MessageSquare }[],
  unread?: number,
) {
  return items.map((item) => ({
    key: item.path,
    icon: <item.icon size={16} />,
    label:
      item.path === '/' && unread ? (
        <span className="inline-flex items-center gap-2">
          {item.label}
          <Badge count={unread} size="small" />
        </span>
      ) : (
        item.label
      ),
  }))
}

export default function Layout() {
  const { agent, logout, setStatus } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [unread, setUnread] = useState(0)
  const { token } = antdTheme.useToken()
  const seeWorkbench = canSee(agent, 'workbench')
  const seeSettings = canSee(agent, 'settings')
  const csNav = CS_NAV.filter((item) => canSee(agent, item.module))
  const matrixNav = MATRIX_NAV.filter((item) => canSee(agent, item.module))
  const required = moduleForPath(location.pathname)
  const allowed = !required || canSee(agent, required)
  const fallback = firstVisiblePath(agent)
  const { theme, toggle: toggleTheme } = useTheme()

  useEffect(() => {
    if (!seeWorkbench) return
    let alive = true
    const load = () =>
      api.statsOverview().then((s) => alive && setUnread(s.total_unread)).catch(() => {})
    load()
    const timer = setInterval(load, 30000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [seeWorkbench])

  const selected = location.pathname.startsWith('/settings')
    ? '/settings'
    : location.pathname.startsWith('/accounts')
      ? '/accounts'
      : location.pathname

  const groups = [
    csNav.length
      ? { type: 'group' as const, label: '客服', children: menuItems(csNav, unread) }
      : null,
    matrixNav.length
      ? { type: 'group' as const, label: '矩阵', children: menuItems(matrixNav) }
      : null,
    seeSettings
      ? {
          type: 'group' as const,
          label: '系统',
          children: menuItems([{ path: '/settings', label: '设置', icon: SettingsIcon }]),
        }
      : null,
  ].filter((item) => item != null)

  const curStatus = STATUS_MAP[agent?.status || 'offline']

  return (
    <AntLayout style={{ height: '100%' }}>
      <Sider width={200} style={{ background: token.colorBgContainer, borderRight: `1px solid ${token.colorBorderSecondary}` }}>
        <div className="h-full flex flex-col">
          <div className="flex items-center gap-2 px-4 h-14 shrink-0">
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center text-white"
              style={{ background: token.colorPrimary }}
            >
              <Sparkles size={16} />
            </div>
            <span className="font-medium" style={{ color: token.colorText }}>智能客服</span>
          </div>
          <div className="flex-1 overflow-auto">
            <Menu
              mode="inline"
              selectedKeys={[selected]}
              items={groups}
              style={{ borderInlineEnd: 'none' }}
              onClick={({ key }) => navigate(key)}
            />
          </div>
          <div className="p-3 flex items-center gap-2 border-t" style={{ borderColor: token.colorBorderSecondary }}>
            <Dropdown
              menu={{
                items: (['active', 'resting'] as const).map((s) => ({
                  key: s,
                  label: (
                    <span className="inline-flex items-center gap-2">
                      <span className="w-2 h-2 rounded-full" style={{ background: STATUS_MAP[s].color }} />
                      {STATUS_MAP[s].label}
                      {agent?.status === s ? ' ✓' : ''}
                    </span>
                  ),
                })),
                onClick: ({ key }) => setStatus(key),
              }}
            >
              <Button type="text" className="flex-1 !justify-start !px-1">
                <span className="inline-flex items-center gap-2 min-w-0">
                  <span
                    className="w-7 h-7 rounded-full text-white text-xs flex items-center justify-center shrink-0"
                    style={{ background: token.colorPrimary }}
                  >
                    {agent?.display_name?.slice(0, 1) || '客'}
                  </span>
                  <span className="truncate text-left">
                    <span className="block text-xs leading-tight">{agent?.display_name || '客服'}</span>
                    <span className="block text-[11px] opacity-60">{curStatus.label}</span>
                  </span>
                </span>
              </Button>
            </Dropdown>
            <Button
              type="text"
              aria-label={theme === 'dark' ? '切换到亮色' : '切换到暗色'}
              icon={theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
              onClick={toggleTheme}
            />
            <Button
              type="text"
              aria-label="退出登录"
              icon={<LogOut size={16} />}
              onClick={() => {
                logout()
                navigate('/login')
              }}
            />
          </div>
        </div>
      </Sider>
      <Content style={{ minWidth: 0, background: token.colorBgLayout }}>
        {allowed ? (
          <Outlet />
        ) : fallback ? (
          <Navigate to={fallback} replace />
        ) : (
          <div className="h-full flex items-center justify-center text-sm" style={{ color: token.colorTextSecondary }}>
            当前账号没有可访问的模块，请联系管理员
          </div>
        )}
      </Content>
    </AntLayout>
  )
}
