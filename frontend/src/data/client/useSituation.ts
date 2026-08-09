/**
 * The cycle-level assessment: what the current set of tracks means together.
 *
 * Polled rather than streamed. It is a small, whole-picture summary that changes on the scale
 * of seconds, and giving it its own WebSocket subscription would add a second liveness state
 * for an operator to reason about — "is the situation banner live?" — for no benefit over a
 * short poll.
 *
 * A failed poll leaves the previous finding in place and reports the error rather than
 * clearing to "nothing found". An unreachable backend is not evidence that a coordinated
 * approach has stopped, and blanking the banner would say exactly that.
 */

import { useEffect, useState } from 'react'

import { api } from './api'
import type { SituationView } from '../models/api'

const POLL_INTERVAL_MS = 2_000

export interface SituationState {
  readonly situation: SituationView | null
  readonly error: string | null
}

export function useSituation(): SituationState {
  const [situation, setSituation] = useState<SituationView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    let cancelled = false

    async function poll() {
      try {
        const next = await api.situation(abort.signal)
        if (cancelled) return
        setSituation(next)
        setError(null)
      } catch (cause) {
        if (cancelled || abort.signal.aborted) return
        // Deliberately does not clear `situation`. See the module note.
        setError(String(cause))
      }
    }

    void poll()
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS)

    return () => {
      cancelled = true
      abort.abort()
      window.clearInterval(timer)
    }
  }, [])

  return { situation, error }
}
