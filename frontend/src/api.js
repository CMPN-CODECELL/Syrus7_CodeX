import { useEffect, useRef, useState } from 'react'

let token = localStorage.getItem('ts_token') || ''
export const getToken = () => token
export const setToken = (t) => {
  token = t
  if (t) localStorage.setItem('ts_token', t)
  else localStorage.removeItem('ts_token')
}

export async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: body ? JSON.stringify(body) : undefined,
  })
  const isAuthCall = path.startsWith('/login') || path.startsWith('/register')
  if (res.status === 401 && !isAuthCall) {
    setToken('')
    window.location.reload()
  }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`)
  return data
}

/** Live snapshot of the platform pushed over a WebSocket (reconnects automatically). */
export function useLive(tok) {
  const [snap, setSnap] = useState(null)
  const [connected, setConnected] = useState(false)
  useEffect(() => {
    let ws, timer, closed = false
    const connect = () => {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${window.location.host}/ws?token=${tok}`)
      ws.onopen = () => setConnected(true)
      ws.onmessage = (m) => {
        const d = JSON.parse(m.data)
        if (d.type === 'snapshot') setSnap(d.data)
      }
      ws.onclose = () => {
        setConnected(false)
        if (!closed) timer = setTimeout(connect, 1500)
      }
    }
    connect()
    return () => {
      closed = true
      clearTimeout(timer)
      if (ws) ws.close()
    }
  }, [tok])
  return { snap, connected }
}

/** Poll a REST endpoint every `ms` milliseconds. */
export function usePoll(path, ms = 2000, enabled = true) {
  const [data, setData] = useState(null)
  const pathRef = useRef(path)
  pathRef.current = path
  useEffect(() => {
    if (!enabled) return undefined
    let stop = false
    const run = async () => {
      try {
        const d = await api(pathRef.current)
        if (!stop) setData(d)
      } catch (e) { /* ignore transient errors */ }
    }
    run()
    const id = setInterval(run, ms)
    return () => { stop = true; clearInterval(id) }
  }, [path, ms, enabled])
  return data
}

export const money = (n) => {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  const s = Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return `${n < 0 ? '-' : ''}\u20B9${s}`
}
export const tone = (n) => (n > 0 ? 'text-up' : n < 0 ? 'text-down' : 'text-muted')
export const signed = (n) => (n > 0 ? `+${n}` : `${n}`)
export const cx = (...a) => a.filter(Boolean).join(' ')
export const clock = (ts) => new Date(ts * 1000).toLocaleTimeString()
