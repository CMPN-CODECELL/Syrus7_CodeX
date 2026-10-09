import { useEffect, useRef, useState } from 'react'
import { cx, money } from '../api.js'

/** Live price strip with up/down arrows, change since page load, and a short flash on every move. */
export default function Ticker({ prices }) {
  const open = useRef({})
  const prev = useRef({})
  const [dirs, setDirs] = useState({})

  useEffect(() => {
    const next = {}
    Object.entries(prices || {}).forEach(([s, p]) => {
      if (open.current[s] === undefined) open.current[s] = p
      const before = prev.current[s]
      next[s] = before === undefined || p === before ? (dirs[s] || 0) : Math.sign(p - before)
    })
    prev.current = { ...prices }
    setDirs(next)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prices])

  return (
    <div className="flex items-center gap-5 overflow-x-auto no-scrollbar">
      {Object.entries(prices || {}).map(([s, p]) => {
        const d = dirs[s] || 0
        const chg = open.current[s] ? ((p - open.current[s]) / open.current[s]) * 100 : 0
        return (
          <div key={s} className="flex items-baseline gap-2 whitespace-nowrap">
            <span className="text-xs font-semibold text-muted">{s}</span>
            <span key={p} className={cx('text-sm tabular-nums font-medium px-1 rounded', d > 0 && 'flash-up', d < 0 && 'flash-down')}>
              {money(p).replace('\u20B9', '')}
            </span>
            <span className={cx('text-xs tabular-nums', chg > 0 ? 'text-up' : chg < 0 ? 'text-down' : 'text-muted')}>
              {d > 0 ? '\u25B2' : d < 0 ? '\u25BC' : ''} {chg >= 0 ? '+' : ''}{chg.toFixed(2)}%
            </span>
          </div>
        )
      })}
    </div>
  )
}
