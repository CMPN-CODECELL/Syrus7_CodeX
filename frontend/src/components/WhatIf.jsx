import { useEffect, useRef, useState } from 'react'
import { api, cx, money, signed, tone } from '../api.js'

const Meter = ({ used, limit, warn }) => {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0
  return (
    <div className="h-1.5 w-full bg-line rounded">
      <div className={cx('h-1.5 rounded', pct >= 100 ? 'bg-down' : pct >= 70 || warn ? 'bg-warn' : 'bg-up')} style={{ width: `${pct}%` }} />
    </div>
  )
}

function Row({ k, children }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 border-b border-line/60 last:border-0">
      <span className="text-xs text-muted">{k}</span>
      <span className="text-sm tabular-nums text-right">{children}</span>
    </div>
  )
}

const Arrow = ({ a, b, fmt = (v) => v }) => (
  <span>{fmt(a)} <span className="text-faint">{'\u2192'}</span> <span className="font-medium">{fmt(b)}</span></span>
)

/** Pure view of a what-if result (also used by the render smoke test). */
export function WhatIfResult({ r }) {
  const v = r.verdict, im = r.impact
  return (
    <div className="space-y-4">
      <div className={cx('rounded-lg border px-3 py-2.5', v.allowed ? 'border-up/40 bg-up/10' : 'border-down/40 bg-down/10')}>
        <div className={cx('text-sm font-semibold', v.allowed ? 'text-up' : 'text-down')}>
          {v.allowed ? 'Would be ALLOWED' : 'Would be BLOCKED'}
          <span className="font-normal text-muted"> · {r.side} {r.qty} {r.symbol} @ {money(r.price)}</span>
        </div>
        <div className="text-xs text-muted mt-0.5">{v.allowed ? 'All risk checks pass.' : v.message}</div>
      </div>

      <div>
        <div className="label mb-1">Risk checks</div>
        <ul className="space-y-2">
          {r.checks.map((c) => (
            <li key={c.code}>
              <div className="flex items-start gap-2">
                <span className={cx('mt-0.5 text-xs font-bold w-4', c.ok ? 'text-up' : 'text-down')}>{c.ok ? '\u2713' : '\u2717'}</span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm">{c.label}</div>
                  <div className="text-xs text-muted">{c.detail}</div>
                  {c.limit !== undefined && <div className="mt-1"><Meter used={c.used} limit={c.limit} /></div>}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>

      <div>
        <div className="label mb-1">If it fills at the current price</div>
        <Row k="Strategy position"><Arrow a={im.position_before} b={im.position_after} fmt={signed} /></Row>
        {im.pending_qty > 0 && <Row k="Pending (unfilled) qty">{im.pending_qty}</Row>}
        <Row k="Average price after">{money(im.avg_price_after)}</Row>
        <Row k="Order value">{money(im.notional)}</Row>
        <Row k="Estimated charges">{money(im.est_charges)}</Row>
        <Row k="Account net position"><Arrow a={im.account_net_before} b={im.account_net_after} fmt={signed} /></Row>
        <Row k="Day P&L"><span className={tone(im.day_pnl_after)}><Arrow a={money(im.day_pnl_now)} b={money(im.day_pnl_after)} /></span></Row>
        <Row k="Loss budget used"><Arrow a={`${im.loss_used_pct_now}%`} b={`${im.loss_used_pct_after}%`} /></Row>
        <Row k="Shield tier"><Arrow a={im.shield_tier_now} b={im.shield_tier_after} /></Row>
      </div>

      <div>
        <div className="label mb-1">Price shock stress test</div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs tabular-nums">
            <thead><tr>{['Move', 'Price', 'Day P&L', 'Loss used', ''].map((h) => <th key={h} className="th !px-2">{h}</th>)}</tr></thead>
            <tbody>
              {r.stress.map((s) => (
                <tr key={s.move_pct} className="border-t border-line/60">
                  <td className={cx('px-2 py-1.5 font-medium', s.move_pct > 0 ? 'text-up' : 'text-down')}>{s.move_pct > 0 ? '+' : ''}{s.move_pct}%</td>
                  <td className="px-2">{s.price.toFixed(2)}</td>
                  <td className={cx('px-2', tone(s.day_pnl))}>{money(s.day_pnl)}</td>
                  <td className="px-2">{s.loss_used_pct}%</td>
                  <td className="px-2">
                    {s.breach ? <span className="chip border-down/40 text-down bg-down/10">limit hit</span>
                      : s.shield_tier > 0 ? <span className="chip border-warn/40 text-warn bg-warn/10">shield {s.shield_tier}</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {r.adverse_move_to_limit && (
          <p className="text-xs text-muted mt-2">
            A {r.adverse_move_to_limit.pct}% move {r.adverse_move_to_limit.direction} would use up this strategy's remaining daily-loss budget.
          </p>
        )}
      </div>
    </div>
  )
}

function KillPreview() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = async () => { try { setErr(''); setData(await api('/what-if/kill')) } catch (e) { setErr(e.message) } }
  return (
    <div className="border-t border-line pt-3">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">Kill switch preview</div>
        <button className="btn" onClick={load}>Preview</button>
      </div>
      <p className="text-xs text-muted mt-1">See what pressing the kill switch right now would do. Nothing is changed.</p>
      {err && <p className="text-xs text-down mt-2">{err}</p>}
      {data && (
        <div className="mt-2 text-xs space-y-1">
          <div>Would cancel <b>{data.open_orders_to_cancel}</b> open orders and close <b>{data.positions_to_close.length}</b> positions.</div>
          {data.positions_to_close.map((p, i) => (
            <div key={i} className="flex justify-between text-muted tabular-nums">
              <span>{p.strategy}: {p.close_side} {Math.abs(p.qty)} {p.symbol}</span><span>{money(p.notional)}</span>
            </div>
          ))}
          <div className="text-muted">Estimated charges {money(data.total_est_charges)} · slippage {money(data.total_est_slippage)}</div>
        </div>
      )}
    </div>
  )
}

export default function WhatIf({ snap }) {
  const subs = snap.subs
  const symbols = Object.keys(snap.prices)
  const [subId, setSubId] = useState('')
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState('BUY')
  const [qty, setQty] = useState(10)
  const [res, setRes] = useState(null)
  const [err, setErr] = useState('')

  const sub = subs.find((s) => String(s.id) === String(subId)) || subs[0]
  const sid = sub?.id
  const sym = symbol || sub?.symbols?.[0] || symbols[0] || ''
  const params = useRef({})
  params.current = { sid, sym, side, qty: Math.floor(Number(qty)) }

  const run = async () => {
    const p = params.current
    if (!p.sid || !p.sym || !(p.qty >= 1)) { setRes(null); return }
    try {
      setErr('')
      setRes(await api('/what-if', { method: 'POST', body: { subscription_id: p.sid, symbol: p.sym, side: p.side, qty: p.qty } }))
    } catch (e) { setErr(e.message); setRes(null) }
  }

  // live preview: re-run shortly after any input changes, and every few seconds so numbers stay fresh
  useEffect(() => { const t = setTimeout(run, 350); return () => clearTimeout(t) }, [sid, sym, side, qty])
  useEffect(() => { const id = setInterval(run, 4000); return () => clearInterval(id) }, [])

  return (
    <section className="panel h-full">
      <div className="panel-title">What-If Risk Simulator
        <span className="ml-auto text-[11px] font-normal text-muted">preview only, nothing is sent</span>
      </div>
      <div className="p-4 space-y-4">
        {subs.length === 0 ? (
          <p className="text-sm text-muted">Subscribe to a strategy first, then test hypothetical orders against its limits.</p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2">
              <label className="col-span-2 text-xs text-muted">Strategy
                <select className="input w-full mt-1" value={sid} onChange={(e) => { setSubId(e.target.value); setSymbol('') }}>
                  {subs.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </label>
              <label className="text-xs text-muted">Symbol
                <select className="input w-full mt-1" value={sym} onChange={(e) => setSymbol(e.target.value)}>
                  {symbols.map((s) => <option key={s}>{s}</option>)}
                </select>
              </label>
              <label className="text-xs text-muted">Quantity
                <input className="input w-full mt-1" type="number" min="1" value={qty} onChange={(e) => setQty(e.target.value)} />
              </label>
              <div className="col-span-2 grid grid-cols-2 gap-2">
                <button onClick={() => setSide('BUY')} className={cx('py-2 rounded-md text-sm font-medium border', side === 'BUY' ? 'bg-up text-white border-up' : 'border-line text-muted bg-panel2')}>Buy</button>
                <button onClick={() => setSide('SELL')} className={cx('py-2 rounded-md text-sm font-medium border', side === 'SELL' ? 'bg-down text-white border-down' : 'border-line text-muted bg-panel2')}>Sell</button>
              </div>
            </div>
            {err && <p className="text-sm text-down">{err}</p>}
            {res ? <WhatIfResult r={res} /> : !err && <p className="text-sm text-muted">Enter a quantity to see the impact.</p>}
          </>
        )}
        <KillPreview />
      </div>
    </section>
  )
}
