import { useState } from 'react'
import { clock, cx, money, usePoll } from '../api.js'

const ST = {
  FILLED: 'text-up', PARTIAL: 'text-fg', OPEN: 'text-fg', SENT: 'text-muted', NEW: 'text-muted',
  UNKNOWN: 'text-warn', BLOCKED: 'text-down', REJECTED: 'text-down', FAILED: 'text-down',
  CANCELLED: 'text-muted', EXPIRED: 'text-muted',
}

export default function Tables({ snap }) {
  const [tab, setTab] = useState('activity')
  const [cat, setCat] = useState('')
  const orders = usePoll('/orders?limit=60', 2000, tab === 'orders')
  const trades = usePoll('/trades?limit=60', 2000, tab === 'trades')
  const events = usePoll(`/events?limit=80${cat ? `&category=${cat}` : ''}`, 2000, tab === 'activity')
  const name = (id) => snap.subs.find((s) => s.id === id)?.name || (id === 0 ? 'Unattributed' : `#${id}`)

  const tabBtn = (id, label) => (
    <button key={id} onClick={() => setTab(id)}
      className={cx('px-3 py-3 text-sm border-b-2 -mb-px', tab === id ? 'border-accent text-fg font-medium' : 'border-transparent text-muted hover:text-fg')}>{label}</button>
  )

  return (
    <section className="panel overflow-hidden">
      <div className="px-3 border-b border-line flex items-center gap-1">
        {tabBtn('activity', 'Activity log')}{tabBtn('orders', 'Orders')}{tabBtn('trades', 'Trades')}
        {tab === 'activity' && (
          <select className="input ml-auto !py-1" value={cat} onChange={(e) => setCat(e.target.value)}>
            <option value="">All</option><option value="decision">Decisions</option><option value="incident">Incidents</option>
          </select>
        )}
      </div>
      <div className="overflow-x-auto max-h-[28rem] overflow-y-auto">
        {tab === 'activity' && (
          <ul>
            {(events || []).map((e) => (
              <li key={e.id} className="flex items-start gap-3 px-4 py-2 border-t border-line first:border-0">
                <span className="text-xs text-faint tabular-nums pt-0.5 shrink-0">{clock(e.ts)}</span>
                <span className={cx('chip shrink-0 !py-0 mt-0.5',
                  e.category === 'incident' ? 'border-warn/40 bg-warn/10 text-warn' : e.kind === 'BLOCK' ? 'border-down/40 bg-down/10 text-down' : 'border-line text-muted')}>{e.kind}</span>
                <span className="text-sm min-w-0 break-words">{e.message}</span>
              </li>
            ))}
            {events && events.length === 0 && <li className="p-4 text-sm text-muted">Nothing yet.</li>}
          </ul>
        )}
        {tab === 'orders' && (
          <table className="w-full tabular-nums">
            <thead><tr>{['Time', 'Strategy', 'Side', 'Symbol', 'Qty', 'Filled', 'Avg', 'Status', 'Why'].map((h, i) => <th key={h} className={cx('th', i >= 6 && i !== 7 && 'hidden md:table-cell')}>{h}</th>)}</tr></thead>
            <tbody>
              {(orders || []).map((o) => (
                <tr key={o.client_order_id} className="border-t border-line">
                  <td className="td text-faint">{clock(o.created_ts)}</td><td className="td">{name(o.sub_id)}</td>
                  <td className={cx('td font-medium', o.side === 'BUY' ? 'text-up' : 'text-down')}>{o.side}</td>
                  <td className="td">{o.symbol}</td><td className="td">{o.qty}</td><td className="td">{o.filled_qty}</td>
                  <td className="td hidden md:table-cell">{o.avg_price ? o.avg_price.toFixed(2) : '-'}</td>
                  <td className={cx('td', ST[o.status])}>{o.status}</td>
                  <td className="td text-xs text-muted max-w-md truncate hidden md:table-cell" title={o.reason}>{o.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {tab === 'trades' && (
          <table className="w-full tabular-nums">
            <thead><tr>{['Time', 'Strategy', 'Side', 'Symbol', 'Qty', 'Price', 'Charges', 'Gross', 'Net'].map((h, i) => <th key={h} className={cx('th', (i === 6 || i === 7) && 'hidden md:table-cell')}>{h}</th>)}</tr></thead>
            <tbody>
              {(trades || []).map((f) => (
                <tr key={f.fill_id} className="border-t border-line">
                  <td className="td text-faint">{clock(f.ts)}</td><td className="td">{name(f.sub_id)}</td>
                  <td className={cx('td font-medium', f.side === 'BUY' ? 'text-up' : 'text-down')}>{f.side}</td>
                  <td className="td">{f.symbol}</td><td className="td">{f.qty}</td><td className="td">{f.price.toFixed(2)}</td>
                  <td className="td hidden md:table-cell">{money(f.charges)}</td><td className="td hidden md:table-cell">{money(f.realized_pnl)}</td>
                  <td className={cx('td', f.realized_pnl - f.charges >= 0 ? 'text-up' : 'text-down')}>{money(f.realized_pnl - f.charges)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}
