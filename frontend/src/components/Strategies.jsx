import { useState } from 'react'
import { api, cx, money, signed, tone } from '../api.js'

const STATUS = {
  ACTIVE: 'border-up/40 text-up bg-up/10',
  STOPPED: 'border-line text-muted bg-panel2',
  HALTED_RISK: 'border-down/40 text-down bg-down/10',
  HALTED_ERROR: 'border-down/40 text-down bg-down/10',
  HALTED_KILL: 'border-warn/40 text-warn bg-warn/10',
}
const LABEL = { ACTIVE: 'Active', STOPPED: 'Paused', HALTED_RISK: 'Halted by risk', HALTED_ERROR: 'Halted (error)', HALTED_KILL: 'Halted (kill switch)' }

const Bar = ({ pct, warn }) => (
  <div className="h-1.5 w-full bg-line rounded">
    <div className={cx('h-1.5 rounded', pct >= 100 ? 'bg-down' : warn ? 'bg-warn' : 'bg-faint')} style={{ width: `${Math.min(100, pct)}%` }} />
  </div>
)

const Status = ({ s }) => <span className={cx('chip', STATUS[s.status] || STATUS.STOPPED)}>{LABEL[s.status] || s.status}</span>

function useActions(s) {
  const [msg, setMsg] = useState('')
  const run = async (fn) => { try { setMsg(''); await fn(); setMsg('Saved') } catch (e) { setMsg(e.message) } }
  const toggle = () => run(() => (s.status === 'ACTIVE'
    ? api(`/subscriptions/${s.id}`, { method: 'DELETE' })
    : api(`/subscriptions/${s.id}/resume`, { method: 'POST' })))
  return { msg, run, toggle }
}

function Settings({ s, intervals, run, msg }) {
  const [lim, setLim] = useState(s.limits)
  const [interval, setIv] = useState(s.params.interval || '')
  const [qty, setQty] = useState(s.params.qty ?? 10)
  const saveLimits = () => run(() => api(`/subscriptions/${s.id}/limits`, {
    method: 'PUT', body: { max_daily_loss: Number(lim.max_daily_loss), max_position: Number(lim.max_position), max_orders_per_min: Number(lim.max_orders_per_min) } }))
  const saveParams = () => run(() => api(`/subscriptions/${s.id}/params`, {
    method: 'PUT', body: { params: { ...(interval ? { interval } : {}), qty: Number(qty) } } }))
  return (
    <div className="space-y-4">
      <div>
        <div className="label mb-1">Risk limits (enforced by the platform)</div>
        <div className="grid grid-cols-3 gap-2 items-end">
          {[['max_daily_loss', 'Max daily loss'], ['max_position', 'Max position'], ['max_orders_per_min', 'Max orders/min']].map(([k, label]) => (
            <label key={k} className="text-xs text-muted">{label}
              <input className="input w-full mt-1" type="number" value={lim[k]} onChange={(e) => setLim({ ...lim, [k]: e.target.value })} />
            </label>
          ))}
        </div>
        <button className="btn-primary mt-2" onClick={saveLimits}>Save limits</button>
      </div>
      <div>
        <div className="label mb-1">Strategy settings</div>
        <div className="flex gap-2 items-end flex-wrap">
          {s.params.interval !== undefined && (
            <label className="text-xs text-muted">Candle interval
              <select className="input block mt-1 w-28" value={interval} onChange={(e) => setIv(e.target.value)}>
                {[...new Set([interval, ...intervals])].filter(Boolean).map((i) => <option key={i}>{i}</option>)}
              </select>
            </label>
          )}
          <label className="text-xs text-muted">Order qty
            <input className="input block mt-1 w-24" type="number" value={qty} onChange={(e) => setQty(e.target.value)} />
          </label>
          <button className="btn-primary" onClick={saveParams}>Save</button>
        </div>
      </div>
      {msg && <div className="text-xs text-muted">{msg}</div>}
    </div>
  )
}

function Positions({ s }) {
  if (s.positions.length === 0) return <span className="text-faint">flat</span>
  return s.positions.map((p) => (
    <div key={p.symbol} className="tabular-nums">
      {p.symbol} <span className={tone(p.qty)}>{signed(p.qty)}</span> <span className="text-xs text-faint">@ {p.avg_price}</span>
    </div>
  ))
}

function RiskLine({ s }) {
  const e = s.effective
  return (
    <>
      <div className="text-xs text-muted mb-1">Loss used {e.loss_used_pct}% of {money(s.limits.max_daily_loss)}</div>
      <Bar pct={e.loss_used_pct} warn={e.loss_used_pct >= 50} />
      <div className="text-xs text-muted mt-1.5">
        orders {s.orders_last_min}/{e.max_orders_per_min} per min · max pos {e.max_position}
        {e.shield_tier > 0 && <span className="chip ml-2 border-warn/40 text-warn bg-warn/10 !py-0">shield {e.shield_tier}</span>}
      </div>
    </>
  )
}

function DesktopRow({ s, intervals }) {
  const [open, setOpen] = useState(false)
  const { msg, run, toggle } = useActions(s)
  return (
    <>
      <tr className="border-t border-line align-top">
        <td className="td">
          <div className="font-medium">{s.name}</div>
          <div className="text-xs text-muted">{s.symbols.join(', ')}{s.params.interval ? ` · ${s.params.interval} candles` : ''}</div>
        </td>
        <td className="td">
          <Status s={s} />
          {s.halt_reason && s.status !== 'ACTIVE' && <div className="text-xs text-muted mt-1 max-w-[220px] whitespace-normal">{s.halt_reason}</div>}
        </td>
        <td className="td"><Positions s={s} /></td>
        <td className="td tabular-nums">
          <div className={cx('font-medium', tone(s.pnl.total_net))}>{money(s.pnl.total_net)}</div>
          <div className="text-xs text-faint">realised {money(s.pnl.realized_net)} · charges {money(s.pnl.charges)}</div>
        </td>
        <td className="td w-64 whitespace-normal"><RiskLine s={s} /></td>
        <td className="td text-right">
          <button className="btn mr-2" onClick={toggle}>{s.status === 'ACTIVE' ? 'Pause' : 'Resume'}</button>
          <button className="btn" onClick={() => setOpen(!open)}>{open ? 'Close' : 'Settings'}</button>
        </td>
      </tr>
      {open && (
        <tr className="bg-panel2 border-t border-line"><td className="td whitespace-normal" colSpan={6}>
          <Settings s={s} intervals={intervals} run={run} msg={msg} />
        </td></tr>
      )}
    </>
  )
}

function MobileCard({ s, intervals }) {
  const [open, setOpen] = useState(false)
  const { msg, run, toggle } = useActions(s)
  return (
    <div className="p-4 border-t border-line first:border-0">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-medium">{s.name}</div>
          <div className="text-xs text-muted">{s.symbols.join(', ')}{s.params.interval ? ` · ${s.params.interval}` : ''}</div>
        </div>
        <Status s={s} />
      </div>
      {s.halt_reason && s.status !== 'ACTIVE' && <div className="text-xs text-muted mt-1">{s.halt_reason}</div>}
      <div className="flex items-end justify-between mt-3">
        <div className="text-sm"><div className="label">Position</div><Positions s={s} /></div>
        <div className="text-right">
          <div className="label">Net P&amp;L</div>
          <div className={cx('text-lg font-semibold tabular-nums', tone(s.pnl.total_net))}>{money(s.pnl.total_net)}</div>
        </div>
      </div>
      <div className="mt-3"><RiskLine s={s} /></div>
      <div className="flex gap-2 mt-3">
        <button className="btn flex-1" onClick={toggle}>{s.status === 'ACTIVE' ? 'Pause' : 'Resume'}</button>
        <button className="btn flex-1" onClick={() => setOpen(!open)}>{open ? 'Close' : 'Settings'}</button>
      </div>
      {open && <div className="mt-3 pt-3 border-t border-line"><Settings s={s} intervals={intervals} run={run} msg={msg} /></div>}
    </div>
  )
}

export function MyStrategies({ snap }) {
  return (
    <section className="panel overflow-hidden">
      <div className="panel-title">My strategies</div>
      {snap.subs.length === 0 ? (
        <p className="p-4 text-sm text-muted">You are not subscribed to anything yet. Pick a strategy below.</p>
      ) : (
        <>
          <div className="hidden lg:block overflow-x-auto">
            <table className="w-full">
              <thead><tr>{['Strategy', 'Status', 'Position', 'Net P&L', 'Risk usage', ''].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
              <tbody>{snap.subs.map((s) => <DesktopRow key={s.id} s={s} intervals={snap.intervals} />)}</tbody>
            </table>
          </div>
          <div className="lg:hidden">{snap.subs.map((s) => <MobileCard key={s.id} s={s} intervals={snap.intervals} />)}</div>
        </>
      )}
    </section>
  )
}

export function Catalog({ catalog }) {
  const [err, setErr] = useState('')
  const subscribe = async (key) => { try { setErr(''); await api('/subscriptions', { method: 'POST', body: { strategy_key: key } }) } catch (e) { setErr(e.message) } }
  if (!catalog) return null
  return (
    <section className="panel">
      <div className="panel-title">Available strategies</div>
      <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
        {catalog.map((c) => (
          <div key={c.key} className="border border-line rounded-lg p-3 flex flex-col bg-panel2">
            <div className="font-medium text-sm">{c.name}{c.demo && <span className="chip ml-2 border-line text-muted !py-0">stress test</span>}</div>
            <p className="text-xs text-muted mt-1 flex-1">{c.description}</p>
            <div className="text-xs text-faint mt-2">Limits: loss {money(c.default_limits.max_daily_loss)} · pos {c.default_limits.max_position} · {c.default_limits.max_orders_per_min}/min</div>
            <button className="btn mt-3" onClick={() => subscribe(c.key)}>{c.subscription_id ? 'Re-activate' : 'Subscribe'}</button>
          </div>
        ))}
      </div>
      {err && <p className="px-4 pb-3 text-sm text-down">{err}</p>}
    </section>
  )
}
