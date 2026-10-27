/** 主题切换：light / dark，localStorage 持久化，默认跟随系统 */
import { create } from 'zustand'

type Theme = 'light' | 'dark'

const STORAGE_KEY = 'cs_agent_theme'

function initialTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function apply(theme: Theme) {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

interface ThemeState {
  theme: Theme
  toggle: () => void
}

export const useTheme = create<ThemeState>((set, get) => {
  const t = initialTheme()
  apply(t)
  return {
    theme: t,
    toggle: () => {
      const next: Theme = get().theme === 'dark' ? 'light' : 'dark'
      localStorage.setItem(STORAGE_KEY, next)
      apply(next)
      set({ theme: next })
    },
  }
})
