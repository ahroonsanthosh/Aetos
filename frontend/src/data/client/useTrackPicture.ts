/**
 * The live track picture: a REST snapshot kept current by the event stream.
 *
 * This hook is the only place the two data paths meet, and the handover between them is the
 * thing it exists to get right. See `stream.ts` for why sequence numbers rather than
 * timestamps make that handover exact.
 *
 * The picture held here is a *cache of backend state*, never an authority. Nothing in the
 * interface computes a track, decides a track's state, or infers a value the backend did not
 * send. When the two disagree, the backend is right by definition.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type { SiteView, StreamedEvent, TrackView } from '../models/api'
import { LocalFrame } from '../models/geo'
import { rememberRetirement, survivorNamedBy } from '../models/succession'
import { toDisplayTrack, type DisplayTrack } from '../models/track'
import { api, ApiError } from './api'
import { useAuth } from './auth'
import { EventStream, type StreamStatus } from './stream'

export interface TrackPicture {
  readonly tracks: readonly DisplayTrack[]
  readonly site: SiteView | null
  readonly frame: LocalFrame | null
  readonly status: StreamStatus
  readonly error: string | null
  readonly latestSeq: number
  /** Tracks the backend retired as duplicates, mapped to the track that carries on each
   *  object (ADR 0021). See `succession.ts`. */
  readonly mergedInto: ReadonlyMap<string, string>
  readonly reload: () => void
}

export function useTrackPicture(): TrackPicture {
  // The console is only mounted with a session, so a 401 here means the session ended while
  // someone was watching. Handing that to the auth layer is what turns it into a login page
  // rather than a picture that stops updating while still looking live — the most dangerous
  // failure this interface can have, because a frozen display and a quiet sky look identical.
  const { sessionExpired } = useAuth()

  const [tracksById, setTracksById] = useState<Map<string, TrackView>>(new Map())
  const [site, setSite] = useState<SiteView | null>(null)
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const [error, setError] = useState<string | null>(null)
  const [latestSeq, setLatestSeq] = useState(0)
  const [mergedInto, setMergedInto] = useState<ReadonlyMap<string, string>>(new Map())
  const [reloadToken, setReloadToken] = useState(0)

  const streamRef = useRef<EventStream | null>(null)

  const reload = useCallback(() => setReloadToken((n) => n + 1), [])

  useEffect(() => {
    const abort = new AbortController()
    let cancelled = false

    async function bootstrap() {
      try {
        // The site first: without a reference point there is no frame to place a track in, so
        // rendering tracks before it arrives would mean rendering them in the wrong place.
        const sites = await api.sites(abort.signal)
        if (cancelled) return
        setSite(sites.sites[0] ?? null)

        const snapshot = await api.tracks(abort.signal)
        if (cancelled) return

        setTracksById(new Map(snapshot.tracks.map((t) => [t.track_id, t])))
        setLatestSeq(snapshot.as_of_seq)
        setError(null)

        // Open at one past the sequence the snapshot was projected from. Everything committed
        // after that arrives on the stream; everything before it is already in the snapshot.
        const stream = new EventStream(snapshot.as_of_seq, {
          onEvent: applyEvent,
          onStatusChange: setStatus,
          onUnauthenticated: sessionExpired,
        })
        streamRef.current = stream
        stream.start()
      } catch (cause) {
        if (cancelled) return

        if (cause instanceof ApiError && cause.status === 401) {
          sessionExpired()
          return
        }

        setStatus('closed')
        setError(
          cause instanceof ApiError
            ? cause.message
            : `Could not load the track picture: ${String(cause)}`,
        )
      }
    }

    function applyEvent(event: StreamedEvent) {
      setLatestSeq(event.seq)

      if (!event.track_id) return

      // TRACK_LOST is handled before the snapshot branch, and the order matters. Every event
      // that changes a track now carries a full snapshot — including the final one — so
      // testing for a snapshot first would re-add the very track being retired, and the
      // operator would accumulate dead tracks that a page refresh silently cleared.
      //
      // The backend drops a LOST track from its active set; the client mirrors that, so the
      // live picture and a fresh GET /tracks always agree. The track is not destroyed: its
      // whole history remains in the event log and is readable through the history endpoint.
      if (event.event_type === 'TRACK_LOST') {
        const id = event.track_id
        const survivor = survivorNamedBy(event.detail ?? {})
        if (survivor) setMergedInto((current) => rememberRetirement(current, id, survivor))
        setTracksById((current) => {
          const next = new Map(current)
          next.delete(id)
          return next
        })
        return
      }

      // Otherwise the picture is replaced from the snapshot rather than patched. Patching
      // would mean the client reconstructing state from deltas — a second implementation of
      // the backend's own logic, and one that would drift from it.
      if (event.track) {
        const track = event.track
        setTracksById((current) => new Map(current).set(track.track_id, track))
      }
    }

    void bootstrap()

    return () => {
      cancelled = true
      abort.abort()
      streamRef.current?.close()
      streamRef.current = null
    }
  }, [reloadToken, sessionExpired])

  const frame = useMemo(() => (site ? new LocalFrame(site.reference) : null), [site])

  const tracks = useMemo(() => {
    if (!frame) return []
    return [...tracksById.values()]
      .map((view) => toDisplayTrack(view, frame))
      // Stable ordering by identifier. Sorting by a changing quantity would make rows jump
      // under an operator's cursor every update cycle.
      .sort((a, b) => a.id.localeCompare(b.id))
  }, [tracksById, frame])

  return { tracks, site, frame, status, error, latestSeq, mergedInto, reload }
}
