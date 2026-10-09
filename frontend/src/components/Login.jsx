import { useState } from 'react'
import { api, setToken } from '../api.js'
import { Shield } from './Icons.jsx'
import { ThemeToggle } from './Header.jsx'

export default function Login({ onAuth }) {
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const r = await api(mode === 'login' ? '/login' : '/register', { method: 'POST', body: { username, password } })
      setToken(r.token)
      onAuth(r)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4 relative">
      <div className="absolute top-3 right-3"><ThemeToggle /></div>
      <form onSubmit={submit} className="panel w-full max-w-sm p-6 space-y-4 shadow-sm">
        <div className="flex items-center gap-3">
          <Shield width={30} height={30} className="text-accent" />
          <div>
            <h1 className="text-lg font-semibold leading-tight">TradeShield</h1>
            <p className="text-xs text-muted">Algo trading where risk limits always hold.</p>
          </div>
        </div>
        <input className="input w-full !py-2" placeholder="Username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoCapitalize="none" />
        <input className="input w-full !py-2" type="password" placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="text-sm text-down">{error}</p>}
        <button className="btn-primary w-full !py-2" disabled={busy}>{mode === 'login' ? 'Sign in' : 'Create account'}</button>
        <button type="button" className="text-sm text-muted hover:text-fg w-full"
          onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError('') }}>
          {mode === 'login' ? 'New here? Create an account' : 'Already have an account? Sign in'}
        </button>
      </form>
    </div>
  )
}
