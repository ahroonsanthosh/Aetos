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
import { toDisplayTrack, type DisplayTrack } from '../models/track'
import { api, ApiError } from './api'
import { EventStream, type StreamStatus } from './stream'

export interface TrackPicture {
  readonly tracks: readonly DisplayTrack[]
  readonly site: SiteView | null
  readonly frame: LocalFrame | null
  readonly status: StreamStatus
  readonly error: string | null
  readonly latestSeq: number
  readonly reload: () => void
}

export function useTrackPicture(): TrackPicture {
  const [tracksById, setTracksById] = useState<Map<string, TrackView>>(new Map())
  const [site, setSite] = useState<SiteView | null>(null)
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const [error, setError] = useState<string | null>(null)
  const [latestSeq, setLatestSeq] = useState(0)
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
        })
        streamRef.current = stream
        stream.start()
      } catch (cause) {
        if (cancelled) return
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
  }, [reloadToken])

  const frame = useMemo(() => (site ? new LocalFrame(site.reference) : null), [site])

  const tracks = useMemo(() => {
    if (!frame) return []
    return [...tracksById.values()]
      .map((view) => toDisplayTrack(view, frame))
      // Stable ordering by identifier. Sorting by a changing quantity would make rows jump
      // under an operator's cursor every update cycle.
      .sort((a, b) => a.id.localeCompare(b.id))
  }, [tracksById, frame])

  return { tracks, site, frame, status, error, latestSeq, reload }
}
