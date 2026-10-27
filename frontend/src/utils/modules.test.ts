import { describe, expect, it } from 'vitest'
import { canSee, firstVisiblePath, moduleForPath, moduleSummary } from './modules'

describe('module access', () => {
  it('admin and unconfigured agents see every module', () => {
    expect(canSee({ role: 'admin', modules: ['workbench'] }, 'publish')).toBe(true)
    expect(canSee({ role: 'agent', modules: null }, 'settings')).toBe(true)
    expect(canSee({ role: 'agent', modules: ['publish'] }, 'publish')).toBe(true)
    expect(canSee({ role: 'agent', modules: ['publish'] }, 'workbench')).toBe(false)
  })

  it('maps routes and picks the first visible page', () => {
    expect(moduleForPath('/')).toBe('workbench')
    expect(moduleForPath('/settings/ops')).toBe('settings')
    expect(firstVisiblePath({ role: 'agent', modules: ['publish', 'accounts'] })).toBe('/accounts')
    expect(firstVisiblePath({ role: 'agent', modules: [] })).toBeNull()
  })

  it('summarizes a long module list', () => {
    expect(moduleSummary({ role: 'admin', modules: null })).toBe('全部')
    expect(moduleSummary({ role: 'agent', modules: null })).toBe('全部')
    expect(moduleSummary({ role: 'agent', modules: ['workbench', 'knowledge', 'tickets', 'leads'] })).toBe(
      '对话等 4 项',
    )
  })
})
