/**
 * A selected track's recorded history — the route it has actually taken.
 *
 * Deliberately separate from `useTrackPicture`. The live picture is one continuous stream
 * covering every track; history is a point-in-time query scoped to whichever single track an
 * operator has clicked on, and fetching it for every track all the time would be exactly the
 * kind of always-on cost the map's declutter behaviour is meant to avoid. Selecting nothing
 * costs nothing: no request is made, and clearing the selection clears the data immediately
 * rather than leaving a stale trail behind.
 */

import { useEffect, useState } from 'react'

import { api, ApiError } from './api'
import type { LocalFrame } from '../models/geo'
import { toHistoryPoint, type TrackHistoryPoint } from '../models/track'

export interface TrackHistory {
  readonly trackId: string | null
  readonly points: readonly TrackHistoryPoint[]
  readonly loading: boolean
  readonly error: string | null
}

const EMPTY: TrackHistory = { trackId: null, points: [], loading: false, error: null }

/** How often the trail is refetched while a track stays selected, so it keeps growing as new
 *  detections arrive rather than freezing at whatever it looked like the moment it was
 *  clicked. Slower than the live stream on purpose — this is a supplementary view, not the
 *  primary one, and refetching the whole history every second for a long-lived track would
 *  be wasted work for a trail an operator is looking at, not measuring. */
const REFRESH_MS = 4_000

export function useTrackHistory(trackId: string | null, frame: LocalFrame | null): TrackHistory {
  const [history, setHistory] = useState<TrackHistory>(EMPTY)

  useEffect(() => {
    if (!trackId || !frame) {
      // Unselecting clears immediately rather than leaving the previous track's trail
      // rendered underneath nothing — that would be exactly the clutter this is meant to
      // prevent.
      setHistory(EMPTY)
      return
    }

    // Rebound to plain locals so the closure below is narrowed by the guard above rather
    // than needing a cast — `trackId` and `frame` are function parameters, and TypeScript
    // does not carry a narrowing of a mutable parameter into a nested function body.
    const id = trackId
    const localFrame = frame

    const abort = new AbortController()
    let cancelled = false

    async function load(showLoading: boolean) {
      if (showLoading) setHistory((current) => ({ ...current, trackId: id, loading: true }))

      try {
        const response = await api.trackHistory(id, abort.signal)
        if (cancelled) return
        setHistory({
          trackId: id,
          points: response.points.map((point) => toHistoryPoint(point, localFrame)),
          loading: false,
          error: null,
        })
      } catch (cause) {
        if (cancelled) return
        // A track that has since been deleted from the server's perspective is not expected
        // here — history is retained permanently — so any failure is reported rather than
        // silently swallowed. The trail just does not render; the rest of the interface is
        // unaffected.
        setHistory({
          trackId: id,
          points: [],
          loading: false,
          error: cause instanceof ApiError ? cause.message : `Could not load history: ${cause}`,
        })
      }
    }

    void load(true)
    const timer = window.setInterval(() => void load(false), REFRESH_MS)

    return () => {
      cancelled = true
      abort.abort()
      window.clearInterval(timer)
    }
  }, [trackId, frame])

  return history
}
