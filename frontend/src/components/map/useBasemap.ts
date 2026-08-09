/**
 * Whether the geographic basemap is shown, and whether it could be.
 *
 * Separate from `useMapDisplayOptions`, which governs what appears in a track's data block.
 * This is a choice about the display *substrate* rather than about track annotation, and
 * folding the two together would make one preference key mean two unrelated things.
 *
 * Off by default, deliberately. The PPI answers the question a counter-UAS operator is
 * actually asking — range and bearing from the asset — and it renders with no network
 * dependency at all (ADR 0010). The basemap adds terrain context for anyone who wants it
 * without making the default view depend on tiles arriving.
 */

import { useCallback, useEffect, useState } from 'react'

import type { BasemapStatus } from './BasemapLayer'

const STORAGE_KEY = 'aetos.map.basemap'

/**
 * Where the tile archive is served from.
 *
 * A `.pmtiles` file dropped in `frontend/public/` is copied into the build and served by the
 * backend's static mount, so a deployment installs terrain by copying one file — no tile
 * server, no API key, and no network at runtime. See `docs/basemap.md`.
 */
export const BASEMAP_TILES_URL = '/basemap.pmtiles'

/**
 * Glyph fonts for map labels, also local.
 *
 * Optional: without them the terrain still draws, unlabelled. Pointing this at a public font
 * server would put a network dependency back into the one feature built to avoid it — and a
 * dependency that fails quietly, leaving an unexplained blank map.
 */
export const BASEMAP_FONTS_URL = '/basemap-fonts/{fontstack}/{range}.pbf'

export interface BasemapState {
  readonly enabled: boolean
  readonly status: BasemapStatus
  /** Why the basemap is unavailable, when it is. Shown to the operator rather than swallowed:
   *  "no tiles installed" is a deployment fact they can act on. */
  readonly detail: string | null
  readonly toggle: () => void
  readonly report: (status: BasemapStatus, detail?: string) => void
}

export function useBasemap(): BasemapState {
  const [enabled, setEnabled] = useState<boolean>(() => readStored())
  const [status, setStatus] = useState<BasemapStatus>('loading')
  const [detail, setDetail] = useState<string | null>(null)

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(enabled))
    } catch {
      // Storage can be unavailable. Losing the preference across reloads is a minor
      // inconvenience; it must never be the reason the map fails.
    }
  }, [enabled])

  const toggle = useCallback(() => {
    setEnabled((current) => {
      // Reset the status on the way back on, so a previous failure does not persist as a
      // stale explanation of a fresh attempt.
      if (!current) {
        setStatus('loading')
        setDetail(null)
      }
      return !current
    })
  }, [])

  const report = useCallback((next: BasemapStatus, why?: string) => {
    setStatus(next)
    setDetail(why ?? null)
  }, [])

  return { enabled, status, detail, toggle, report }
}

function readStored(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'true'
  } catch {
    return false
  }
}
