import { createContext, useContext, useEffect, useState } from 'react'

const KEY = 'ts_theme'

/** Colours for places where CSS variables cannot be used (Recharts props). */
export const PALETTE = {
  light: { up: '#16a34a', down: '#dc2626', grid: '#e2e8f0', axis: '#64748b', accent: '#2563eb', panel: '#ffffff' },
  dark: { up: '#26c281', down: '#f25555', grid: '#212b38', axis: '#8a99ab', accent: '#4e8cff', panel: '#11171f' },
}

export const ThemeCtx = createContext({ theme: 'light', toggle: () => {}, palette: PALETTE.light })
export const useTheme = () => useContext(ThemeCtx)

export function useThemeState() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem(KEY) || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    } catch (e) { return 'light' }
  })
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    try { localStorage.setItem(KEY, theme) } catch (e) { /* ignore */ }
    const meta = document.querySelector('meta[name=theme-color]')
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#0a0e14' : '#f4f6f9')
  }, [theme])
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))]
}

/** True on phones/tablets (below Tailwind's lg breakpoint). */
export function useIsMobile() {
  const get = () => typeof window !== 'undefined' && window.innerWidth < 1024
  const [m, setM] = useState(get)
  useEffect(() => {
    const on = () => setM(get())
    window.addEventListener('resize', on)
    return () => window.removeEventListener('resize', on)
  }, [])
  return m
}
