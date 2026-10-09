import { useEffect, useRef, useState } from 'react'

const fmtT = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
const UP = 'rgb(var(--up))'
const DOWN = 'rgb(var(--down))'
const T = (k, extra = {}) => ({ fill: `rgb(var(--${k}))`, ...extra })

/** Candlestick chart drawn as SVG: green = close above open, red = close below open. Wicks, volume, crosshair. */
export default function CandleChart({ candles, height = 340 }) {
  const wrap = useRef(null)
  const [w, setW] = useState(640)
  const [hover, setHover] = useState(null)

  useEffect(() => {
    const el = wrap.current
    if (!el) return undefined
    const measure = () => setW(Math.max(260, Math.floor(el.clientWidth)))
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => window.removeEventListener('resize', measure)
    }
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const n = candles.length
  const padL = 6, padR = 62, padT = 8, padB = 20
  const volH = Math.round(height * 0.15)
  const priceH = height - padT - padB - volH - 8
  const plotW = w - padL - padR

  let body = (
    <div className="h-full flex items-center justify-center text-sm text-muted">Waiting for candles to close...</div>
  )

  if (n >= 2) {
    const hi = Math.max(...candles.map((c) => c.high))
    const lo = Math.min(...candles.map((c) => c.low))
    const pad = (hi - lo) * 0.08 || 1
    const yMax = hi + pad, yMin = lo - pad
    const y = (p) => padT + ((yMax - p) / (yMax - yMin)) * priceH
    const slot = plotW / Math.max(n, 40)                 // few candles stay narrow; the newest is always on the right
    const x = (i) => padL + slot * i + slot / 2
    const bodyW = Math.max(1.6, slot * 0.7)
    const vMax = Math.max(...candles.map((c) => c.volume)) || 1
    const last = candles[n - 1]
    const lastUp = last.close >= last.open
    const grid = [0, 1, 2, 3, 4, 5].map((k) => yMin + ((yMax - yMin) * k) / 5)
    const fractions = w < 420 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1]       // fewer labels on phones
    const timeIdx = [...new Set(fractions.map((f) => Math.round((n - 1) * f)))]
    const tickBased = last.interval && last.interval.endsWith('t')
    const hc = candles[hover === null ? n - 1 : Math.min(hover, n - 1)]
    const hcUp = hc.close >= hc.open

    const onMove = (clientX, el) => {
      const r = el.getBoundingClientRect()
      const i = Math.floor((clientX - r.left - padL) / slot)
      setHover(i >= 0 && i < n ? i : null)
    }

    body = (
      <svg width={w} height={height} role="img" aria-label="Candlestick chart" style={{ touchAction: 'pan-y', display: 'block' }}
        onMouseMove={(e) => onMove(e.clientX, e.currentTarget)} onMouseLeave={() => setHover(null)}
        onTouchMove={(e) => e.touches[0] && onMove(e.touches[0].clientX, e.currentTarget)} onTouchEnd={() => setHover(null)}>
        {grid.map((p) => (
          <g key={p}>
            <line x1={padL} x2={w - padR} y1={y(p)} y2={y(p)} style={{ stroke: 'rgb(var(--line))' }} strokeWidth="1" />
            <text x={w - padR + 6} y={y(p) + 3.5} fontSize="10.5" style={T('muted')}>{p.toFixed(2)}</text>
          </g>
        ))}

        {candles.map((c, i) => {
          const up = c.close >= c.open
          const col = up ? UP : DOWN
          const top = y(Math.max(c.open, c.close))
          const h = Math.max(1, Math.abs(y(c.open) - y(c.close)))
          return (
            <g key={`${c.start_ts}-${i}`}>
              <line x1={x(i)} x2={x(i)} y1={y(c.high)} y2={y(c.low)} style={{ stroke: col }} strokeWidth="1.2" />
              <rect x={x(i) - bodyW / 2} y={top} width={bodyW} height={h} rx="0.5" style={{ fill: col, stroke: col }} strokeWidth="0.5" />
              <rect x={x(i) - bodyW / 2} y={padT + priceH + 8 + (volH - (c.volume / vMax) * volH)} width={bodyW}
                height={(c.volume / vMax) * volH} style={{ fill: col, opacity: 0.35 }} />
            </g>
          )
        })}

        {timeIdx.map((i, k) => (
          <text key={i} x={x(i)} y={height - 5} fontSize="10.5" style={T('muted')}
            textAnchor={k === 0 ? 'start' : k === timeIdx.length - 1 ? 'end' : 'middle'}>
            {tickBased ? `#${i + 1}` : fmtT(candles[i].start_ts)}
          </text>
        ))}

        {/* last price line + tag */}
        <line x1={padL} x2={w - padR} y1={y(last.close)} y2={y(last.close)} strokeDasharray="3 3" strokeWidth="1"
          style={{ stroke: lastUp ? UP : DOWN, opacity: 0.8 }} />
        <rect x={w - padR + 2} y={y(last.close) - 8} width={padR - 4} height="16" rx="3" style={{ fill: lastUp ? UP : DOWN }} />
        <text x={w - padR + 6} y={y(last.close) + 3.5} fontSize="10.5" fontWeight="600" fill="#fff">{last.close.toFixed(2)}</text>

        {/* crosshair */}
        {hover !== null && (
          <line x1={x(hover)} x2={x(hover)} y1={padT} y2={padT + priceH + 8 + volH} strokeDasharray="2 3"
            style={{ stroke: 'rgb(var(--muted))' }} strokeWidth="1" />
        )}

        {/* OHLC read-out */}
        <text x={padL + 4} y={padT + 10} fontSize="11" style={T('muted')}>
          <tspan>O </tspan><tspan style={{ fill: hcUp ? UP : DOWN }}>{hc.open.toFixed(2)}</tspan>
          <tspan dx="8">H </tspan><tspan style={{ fill: hcUp ? UP : DOWN }}>{hc.high.toFixed(2)}</tspan>
          <tspan dx="8">L </tspan><tspan style={{ fill: hcUp ? UP : DOWN }}>{hc.low.toFixed(2)}</tspan>
          <tspan dx="8">C </tspan><tspan style={{ fill: hcUp ? UP : DOWN }}>{hc.close.toFixed(2)}</tspan>
          <tspan dx="8">Vol </tspan><tspan style={T('fg')}>{hc.volume}</tspan>
        </text>
      </svg>
    )
  }

  return <div ref={wrap} className="w-full select-none overflow-hidden" style={{ height }}>{body}</div>
}
