import { useState } from 'react'
import { api } from '../api.js'
import { useTheme } from '../theme.js'
import { Moon, Power, Shield, Sun } from './Icons.jsx'
import Ticker from './Ticker.jsx'

export function ThemeToggle({ className = '' }) {
  const { theme, toggle } = useTheme()
  return (
    <button onClick={toggle} className={`btn !px-2 ${className}`} title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      aria-label="Toggle dark and light mode">
      {theme === 'dark' ? <Sun /> : <Moon />}
    </button>
  )
}

export default function Header({ username, connected, snap, onLogout }) {
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  const kill = async () => {
    if (!window.confirm('Kill switch: stop ALL strategies, cancel open orders and close all positions?')) return
    setBusy(true)
    setResult(null)
    try { setResult(await api('/kill', { method: 'POST' })) } catch (e) { setResult({ error: e.message }) }
    setBusy(false)
  }
  const reset = async () => { await api('/kill/reset', { method: 'POST' }); setResult(null) }

  return (
    <>
      <header className="sticky top-0 z-30 bg-panel/95 backdrop-blur border-b border-line">
        <div className="max-w-[1500px] mx-auto px-3 sm:px-4 h-14 flex items-center gap-4">
          <div className="flex items-center gap-2 shrink-0">
            <Shield className="text-accent" />
            <span className="font-semibold tracking-tight">TradeShield</span>
            <span className="hidden sm:flex items-center gap-1.5 text-xs text-muted ml-1">
              <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-up' : 'bg-faint'}`} />
              {connected ? 'Live' : 'Reconnecting'}
            </span>
            <span className={`sm:hidden inline-block w-2 h-2 rounded-full ${connected ? 'bg-up' : 'bg-faint'}`} />
          </div>
          <div className="hidden lg:block flex-1 min-w-0 border-l border-line pl-4"><Ticker prices={snap?.prices} /></div>
          <div className="flex items-center gap-2 ml-auto shrink-0">
            <span className="text-sm text-muted hidden md:inline">{username}</span>
            <ThemeToggle />
            <button className="btn hidden sm:inline-flex" onClick={onLogout}>Sign out</button>
            <button onClick={kill} disabled={busy || snap?.killed}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md bg-down text-white hover:opacity-90 disabled:opacity-50">
              <Power width={15} height={15} />{busy ? 'Stopping...' : 'Kill'}<span className="hidden sm:inline"> switch</span>
            </button>
          </div>
        </div>
        <div className="lg:hidden border-t border-line px-3 py-2"><Ticker prices={snap?.prices} /></div>
      </header>

      {snap?.killed && (
        <div className="bg-down/10 border-b border-down/30 text-down text-sm">
          <div className="max-w-[1500px] mx-auto px-4 py-2 flex items-center justify-between gap-3">
            <span>
              Kill switch is active: trading is stopped.
              {result && !result.error && ` Finished in ${result.elapsed_seconds}s, positions flat: ${result.flat ? 'yes' : 'no'}.`}
              {result?.error && ` ${result.error}`}
            </span>
            <button className="btn shrink-0" onClick={reset}>Reset</button>
          </div>
        </div>
      )}
      {snap?.paused && !snap?.killed && (
        <div className="bg-warn/10 border-b border-warn/30 text-warn text-sm">
          <div className="max-w-[1500px] mx-auto px-4 py-2">Trading paused: {snap.pause_reason || 'reconciling'}...</div>
        </div>
      )}
    </>
  )
}
