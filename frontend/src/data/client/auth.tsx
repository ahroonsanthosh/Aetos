/**
 * Who is logged in, for the whole interface.
 *
 * The first piece of global state in this frontend, and it earns that: the console cannot
 * decide what to render until it knows whether there is a session, and every page needs the
 * answer. Everything else the interface shows is derived from the track picture and is passed
 * down explicitly.
 *
 * ## What this is not
 *
 * It is not a permission system. `session.role` decides what the interface *offers*; what it
 * *permits* is decided by the backend on every request. A viewer who crafts an acknowledge call
 * by hand is refused by `RequireOperator` on the endpoint, not by anything here. Treating a
 * hidden button as a security control is how a client-side check becomes the only check.
 *
 * ## Why `status` has four values and not a boolean
 *
 * "Checking" and "no session" must not render the same way. A boolean starting false shows the
 * login page for a moment to someone who is already logged in, on every single load — which
 * teaches operators that the console randomly logs them out. `unreachable` is separated from
 * `signed-out` for the same reason in the other direction: a backend that is down must not be
 * presented as a credential problem, or the operator spends the outage retyping a password
 * that was never wrong.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import type { SessionView } from '../models/api'
import { ApiError, api } from './api'

export type AuthStatus = 'checking' | 'signed-in' | 'signed-out' | 'unreachable'

export interface AuthState {
  status: AuthStatus
  session: SessionView | null
  /** Why the last sign-in attempt failed, for the form to show. Cleared on the next attempt. */
  error: string | null
  signIn: (username: string, password: string) => Promise<boolean>
  signOut: () => Promise<void>
  /** Called when any request comes back 401, so an expired session lands on the login page
   *  rather than leaving a console that silently stops updating. */
  sessionExpired: () => void
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('checking')
  const [session, setSession] = useState<SessionView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const abort = new AbortController()

    api
      .me(abort.signal)
      .then((found) => {
        setSession(found)
        setStatus('signed-in')
      })
      .catch((cause) => {
        if (abort.signal.aborted) return
        setSession(null)
        // 401 is the expected answer for "not logged in" and is not an error worth showing.
        // Anything else — a network failure, a 500 — is a different problem and must not be
        // presented as one a password can solve.
        setStatus(cause instanceof ApiError && cause.status === 401 ? 'signed-out' : 'unreachable')
      })

    return () => abort.abort()
  }, [])

  const signIn = useCallback(async (username: string, password: string) => {
    setError(null)
    try {
      const found = await api.login(username, password)
      setSession(found)
      setStatus('signed-in')
      return true
    } catch (cause) {
      setSession(null)
      setStatus('signed-out')
      setError(
        cause instanceof ApiError && cause.status === 401
          ? cause.message
          : 'Could not reach the backend.',
      )
      return false
    }
  }, [])

  const signOut = useCallback(async () => {
    try {
      await api.logout()
    } catch {
      // Deliberately ignored. The backend clears the cookie and treats logout as idempotent,
      // and an operator who pressed sign-out must end up signed out of this interface whatever
      // the network did — leaving them on a console they believe they have left is worse than
      // a session row surviving on the server until it expires.
    }
    setSession(null)
    setError(null)
    setStatus('signed-out')
  }, [])

  const sessionExpired = useCallback(() => {
    setSession(null)
    setStatus('signed-out')
    setError('Your session ended. Sign in again.')
  }, [])

  const value = useMemo<AuthState>(
    () => ({ status, session, error, signIn, signOut, sessionExpired }),
    [status, session, error, signIn, signOut, sessionExpired],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext)
  if (value === null) throw new Error('useAuth used outside AuthProvider')
  return value
}
