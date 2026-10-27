import { Menu, theme } from 'antd'
import { ClipboardList, Plug, Sparkles, Users } from 'lucide-react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../../store'

const NAV = [
  { to: '/settings/service', label: '接待', icon: Users },
  { to: '/settings/automation', label: '自动化', icon: Sparkles },
  { to: '/settings/channels', label: '通道', icon: Plug },
  { to: '/settings/ops', label: '运维', icon: ClipboardList, admin: true },
] as const

export default function SettingsLayout() {
  const { agent } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { token } = theme.useToken()
  const items = NAV.filter((item) => !('admin' in item && item.admin) || agent?.role === 'admin')

  return (
    <div className="h-full flex" style={{ background: token.colorBgLayout }}>
      <div
        className="w-44 shrink-0 py-4"
        style={{ background: token.colorBgContainer, borderRight: `1px solid ${token.colorBorderSecondary}` }}
      >
        <div className="px-4 mb-2 font-medium" style={{ color: token.colorText }}>设置</div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          style={{ borderInlineEnd: 'none' }}
          items={items.map((item) => ({
            key: item.to,
            icon: <item.icon size={14} />,
            label: item.label,
          }))}
          onClick={({ key }) => navigate(key)}
        />
      </div>
      <div className="flex-1 overflow-y-auto p-6">
        <Outlet />
      </div>
    </div>
  )
}
