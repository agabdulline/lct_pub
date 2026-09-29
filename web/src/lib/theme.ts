// Theme: dark by default — a dispatcher room (docs/architecture/frontend.md §2); light by the switch.

import { useSyncExternalStore } from 'react'

export type Theme = 'dark' | 'light'

const KEY = 'lct-theme'
const listeners = new Set<() => void>()

function read(): Theme {
  try {
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function applyTheme(theme: Theme = read()) {
  document.documentElement.classList.toggle('dark', theme === 'dark')
  document.documentElement.style.colorScheme = theme
}

export function setTheme(theme: Theme) {
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    // storage may be unavailable — the theme still switches for this page
  }
  applyTheme(theme)
  listeners.forEach((l) => l())
}

export const useTheme = (): Theme =>
  useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    read,
    () => 'dark',
  )
