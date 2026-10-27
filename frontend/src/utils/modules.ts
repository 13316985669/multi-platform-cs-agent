/** 与后端 app/core/modules.py 的模块清单保持一致。 */

export type ModuleGroup = 'cs' | 'matrix' | 'settings'

export type ModuleDef = {
  key: string
  label: string
  group: ModuleGroup
  path: string
}

export const MODULES: ModuleDef[] = [
  { key: 'workbench', label: '对话', group: 'cs', path: '/' },
  { key: 'knowledge', label: '知识库', group: 'cs', path: '/knowledge' },
  { key: 'tickets', label: '工单', group: 'cs', path: '/tickets' },
  { key: 'leads', label: '留资', group: 'cs', path: '/leads' },
  { key: 'dashboard', label: '数据', group: 'cs', path: '/dashboard' },
  { key: 'accounts', label: '账号', group: 'matrix', path: '/accounts' },
  { key: 'studio', label: '创作', group: 'matrix', path: '/studio' },
  { key: 'publish', label: '发布', group: 'matrix', path: '/publish' },
  { key: 'comments', label: '评论', group: 'matrix', path: '/comments' },
  { key: 'funnel', label: '漏斗', group: 'matrix', path: '/funnel' },
  { key: 'analytics', label: '效果', group: 'matrix', path: '/analytics' },
  { key: 'inspiration', label: '灵感', group: 'matrix', path: '/inspiration' },
  { key: 'mock', label: '模拟', group: 'matrix', path: '/mock' },
  { key: 'settings', label: '设置', group: 'settings', path: '/settings' },
]

export const CS_MODULE_KEYS = MODULES.filter((item) => item.group === 'cs').map((item) => item.key)
export const MATRIX_MODULE_KEYS = MODULES.filter((item) => item.group === 'matrix').map((item) => item.key)
export const ALL_MODULE_KEYS = MODULES.map((item) => item.key)

const LABEL = Object.fromEntries(MODULES.map((item) => [item.key, item.label]))

export function moduleLabel(key: string): string {
  return LABEL[key] || key
}

type ModuleAgent = { role: string; modules?: string[] | null } | null | undefined

/** 管理员和未单独配置（modules 为空）的账号可见全部。 */
export function canSee(agent: ModuleAgent, key: string): boolean {
  if (!agent) return false
  if (agent.role === 'admin') return true
  if (agent.modules == null) return true
  return agent.modules.includes(key)
}

export function moduleForPath(pathname: string): string | null {
  if (pathname === '/') return 'workbench'
  const hit = MODULES.find(
    (item) => item.path !== '/' && (pathname === item.path || pathname.startsWith(`${item.path}/`)),
  )
  return hit?.key ?? null
}

export function firstVisiblePath(agent: ModuleAgent): string | null {
  const hit = MODULES.find((item) => canSee(agent, item.key))
  return hit?.path ?? null
}

/** 列表里的可见范围。超过 3 个收成「对话等 N 项」。 */
export function moduleSummary(agent: { role: string; modules?: string[] | null }): string {
  if (agent.role === 'admin' || agent.modules == null) return '全部'
  const labels = agent.modules.map(moduleLabel)
  if (labels.length === 0) return '无'
  if (labels.length <= 3) return labels.join('、')
  return `${labels[0]}等 ${labels.length} 项`
}
