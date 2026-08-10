/**
 * The sign-in screen.
 *
 * Shown instead of the console, never alongside it. A login form overlaid on a live track
 * picture would be showing data to someone who has not authenticated, which is the thing the
 * whole mechanism exists to prevent.
 *
 * The mode banner is kept here deliberately. Whether the data is simulated has to be visible at
 * all times (ADR 0011), and "all times" includes before anyone has logged in — an operator
 * walking up to a console should be able to tell what they are about to look at.
 */

import { useEffect, useState, type FormEvent } from 'react'

import { api } from '../../data/client/api'
import { useAuth } from '../../data/client/auth'
import type { ModeView } from '../../data/models/api'
import './login.css'

export function LoginPage() {
  const { signIn, error, status } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<ModeView | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    // Public by design — it is the liveness surface. Failing quietly is right: an unknown mode
    // renders as unknown rather than blocking the form.
    api.mode(abort.signal).then(setMode).catch(() => undefined)
    return () => abort.abort()
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    await signIn(username.trim(), password)
    setBusy(false)
    // The password is cleared whatever happened. On success it is no longer needed; on failure
    // the next attempt should be typed, not re-submitted from a field the operator has walked
    // away from.
    setPassword('')
  }

  return (
    <div className="login">
      <form className="login__panel" onSubmit={submit}>
        <div className="login__brand">
          AETOS
          <span className="login__subtitle">Counter-UAS / Sensor Fusion</span>
        </div>

        {mode?.simulated && (
          <p className="login__mode">
            Data is <strong>SIMULATED</strong>. No real sensor is connected.
          </p>
        )}

        <label className="login__field">
          <span className="label">Operator</span>
          <input
            className="login__input"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>

        <label className="login__field">
          <span className="label">Password</span>
          <input
            className="login__input"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {/* `role="alert"` so a screen reader announces a failed sign-in. Without it the form
            appears to do nothing, which is indistinguishable from a broken button. */}
        {error && (
          <p className="login__error" role="alert">
            {error}
          </p>
        )}

        {status === 'unreachable' && (
          <p className="login__error" role="alert">
            The backend is not reachable. This is not a credential problem — signing in will not
            work until it is back.
          </p>
        )}

        <button className="login__submit" type="submit" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
