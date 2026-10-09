import { useEffect, useState } from 'react'
import { api, cx, getToken, money, setToken, signed, tone, useLive } from './api.js'
import { PALETTE, ThemeCtx, useThemeState } from './theme.js'
import BottomNav from './components/BottomNav.jsx'
import { ChartPanel, PnlChart } from './components/Charts.jsx'
import DemoTools from './components/DemoTools.jsx'
import Header from './components/Header.jsx'
import Login from './components/Login.jsx'
import { Catalog, MyStrategies } from './components/Strategies.jsx'
import Tables from './components/Tables.jsx'
import WhatIf from './components/WhatIf.jsx'

function Stat({ label, children, sub }) {
  return (
    <div className="panel p-4">
      <div className="label">{label}</div>
      <div className="text-xl font-semibold mt-1 tabular-nums">{children}</div>
      {sub && <div className="text-xs text-faint mt-1">{sub}</div>}
    </div>
  )
}

function Dashboard({ username, onLogout }) {
  const { snap, connected } = useLive(getToken())
  const [catalog, setCatalog] = useState(null)
  const [tab, setTab] = useState('home')

  useEffect(() => {
    const load = () => api('/strategies').then(setCatalog).catch(() => {})
    load()
    const id = setInterval(load, 4000)
    return () => clearInterval(id)
  }, [])

  // On phones only the active tab is shown; from the lg breakpoint up everything is visible at once.
  const show = (id, extra = '') => cx(tab === id ? 'block' : 'hidden', 'lg:block', extra)

  const active = snap ? snap.subs.filter((s) => s.status === 'ACTIVE').length : 0
  const openOrders = snap ? snap.subs.reduce((a, s) => a + s.open_orders, 0) : 0
  const positions = snap ? Object.entries(snap.account_positions).filter(([, q]) => q !== 0) : []

  return (
    <div className="min-h-screen">
      <Header username={username} connected={connected} snap={snap} onLogout={onLogout} />
      <main className="max-w-[1500px] mx-auto px-3 sm:px-4 py-4 pb-24 lg:pb-8">
        {!snap ? <p className="text-sm text-muted">Connecting...</p> : (
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
            <div className={show('home', 'lg:col-span-12')}>
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                <Stat label="Net P&L (after charges)"><span className={tone(snap.total_net)}>{money(snap.total_net)}</span></Stat>
                <Stat label="Active strategies" sub={`${snap.subs.length} subscribed`}>{active}</Stat>
                <Stat label="Open orders">{openOrders}</Stat>
                <Stat label="Account positions" sub="all strategies combined">
                  {positions.length === 0 ? <span className="text-faint text-base">flat</span>
                    : <span className="text-base leading-snug block">{positions.map(([s, q]) => (
                      <span key={s} className="block">{s} <span className={tone(q)}>{signed(q)}</span></span>))}</span>}
                </Stat>
              </div>
            </div>

            <div className={show('chart', 'lg:col-span-8')}><ChartPanel snap={snap} /></div>
            <div className={show('whatif', 'lg:col-span-4')}><WhatIf snap={snap} /></div>

            <div className={show('strategies', 'lg:col-span-12')}><MyStrategies snap={snap} /></div>
            <div className={show('home', 'lg:col-span-12')}><PnlChart /></div>
            <div className={show('activity', 'lg:col-span-12')}><Tables snap={snap} /></div>
            <div className={show('strategies', 'lg:col-span-12')}><Catalog catalog={catalog} /></div>
            <div className={show('activity', 'lg:col-span-12')}><DemoTools snap={snap} /></div>
          </div>
        )}
      </main>
      <BottomNav active={tab} onChange={setTab} />
    </div>
  )
}

export default function App() {
  const [theme, toggle] = useThemeState()
  const [user, setUser] = useState(() => (getToken() ? localStorage.getItem('ts_user') || 'trader' : null))
  const onAuth = (r) => { localStorage.setItem('ts_user', r.username); setUser(r.username) }
  const logout = () => { setToken(''); setUser(null) }
  return (
    <ThemeCtx.Provider value={{ theme, toggle, palette: PALETTE[theme] }}>
      {user ? <Dashboard username={user} onLogout={logout} /> : <Login onAuth={onAuth} />}
    </ThemeCtx.Provider>
  )
}
