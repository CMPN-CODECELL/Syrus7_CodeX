import { useState } from 'react'
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, clock, cx, money, usePoll } from '../api.js'
import { useIsMobile, useTheme } from '../theme.js'
import CandleChart from './CandleChart.jsx'

/** "1m" < "5m" < "1h" < "40t" : time candles first (short to long), then tick candles. */
const weight = (s) => {
  const n = parseInt(s, 10)
  const u = s.slice(-1)
  return u === 't' ? 1e7 + n : n * ({ s: 1, m: 60, h: 3600 }[u] || 1)
}

export function PnlChart() {
  const { palette } = useTheme()
  const data = usePoll('/pnl-history', 3000)
  const pts = (data || []).map((p) => ({ t: clock(p.ts), total: p.total }))
  const lastVal = pts.length ? pts[pts.length - 1].total : 0
  const col = lastVal >= 0 ? palette.up : palette.down
  return (
    <section className="panel h-full">
      <div className="panel-title">Net P&amp;L over time
        <span className={cx('ml-auto tabular-nums', lastVal >= 0 ? 'text-up' : 'text-down')}>{money(lastVal)}</span>
      </div>
      <div className="h-56 p-3">
        {pts.length < 2 ? <p className="text-sm text-muted p-2">Collecting data...</p> : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={pts}>
              <defs>
                <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={col} stopOpacity={0.28} />
                  <stop offset="100%" stopColor={col} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={palette.grid} vertical={false} />
              <XAxis dataKey="t" tick={{ fontSize: 11, fill: palette.axis }} minTickGap={40} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: palette.axis }} width={64} axisLine={false} tickLine={false} />
              <ReferenceLine y={0} stroke={palette.axis} strokeDasharray="3 3" />
              <Tooltip formatter={(v) => money(v)} contentStyle={{ background: palette.panel, border: `1px solid ${palette.grid}`, borderRadius: 8, fontSize: 12 }} />
              <Area type="monotone" dataKey="total" stroke={col} strokeWidth={1.8} fill="url(#pnlFill)" dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  )
}

export function ChartPanel({ snap }) {
  const mobile = useIsMobile()
  const symbols = Object.keys(snap.prices)
  const [symbol, setSymbol] = useState('')
  const [interval, setIv] = useState('1m')
  const [custom, setCustom] = useState('')
  const [msg, setMsg] = useState('')
  const sym = symbol || symbols[0] || ''
  const data = usePoll(`/candles?symbol=${sym}&interval=${interval}&n=60`, 2000, !!sym)

  const rows = data ? [...data.candles, ...(data.current ? [data.current] : [])] : []
  const price = snap.prices[sym]
  const first = rows.length ? rows[0].open : price
  const chg = first ? ((price - first) / first) * 100 : 0
  const specs = [...snap.intervals].sort((a, b) => weight(a) - weight(b))

  const add = async () => {
    try {
      const r = await api('/intervals', { method: 'POST', body: { interval: custom } })
      setIv(r.interval); setCustom(''); setMsg(`Added ${r.interval}`)
    } catch (e) { setMsg(e.message) }
  }

  return (
    <section className="panel h-full">
      <div className="px-4 pt-3 pb-2 flex flex-wrap items-end gap-x-6 gap-y-2">
        <div>
          <select className="input !py-1 font-semibold" value={sym} onChange={(e) => setSymbol(e.target.value)}>
            {symbols.map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
        <div>
          <div className="text-2xl font-semibold tabular-nums leading-none">{price ? money(price) : '-'}</div>
          <div className={cx('text-xs tabular-nums mt-1', chg >= 0 ? 'text-up' : 'text-down')}>
            {chg >= 0 ? '+' : ''}{chg.toFixed(2)}% over the visible candles
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <input className="input w-28 sm:w-36" placeholder="15m, 1h, 100t" value={custom} onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && custom && add()} />
          <button className="btn" onClick={add} disabled={!custom}>Add</button>
        </div>
      </div>
      <div className="px-4 pb-2 flex gap-1.5 overflow-x-auto no-scrollbar">
        {specs.map((s) => (
          <button key={s} onClick={() => setIv(s)}
            className={cx('px-2.5 py-1 text-xs rounded-md border whitespace-nowrap',
              s === interval ? 'bg-accent text-white border-accent' : 'border-line text-muted hover:text-fg bg-panel2')}>{s}</button>
        ))}
      </div>
      {msg && <p className="px-4 pb-1 text-xs text-muted">{msg}</p>}
      <div className="px-1 pb-2"><CandleChart candles={rows} height={mobile ? 280 : 360} /></div>
    </section>
  )
}
