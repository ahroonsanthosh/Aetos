/**
 * Which fields appear in an unselected track's on-map data block.
 *
 * This is a display preference, not backend state — nothing here is fetched, and nothing
 * here changes what any track *is*, only what an operator has chosen to see about it at a
 * glance. That is why it lives beside the map components that use it rather than under
 * `data/client/`, which is reserved for hooks that actually talk to the backend.
 *
 * The selected track always shows every field regardless of these toggles (see TrackMarker):
 * this state only governs the lean, glanceable line under everyone else, which is the thing
 * that actually needs a way to stay uncluttered as an operator chooses what matters to them.
 */

import { useCallback, useEffect, useState } from 'react'

export interface MapDisplayOptions {
  showSpeed: boolean
  showAltitude: boolean
  showHeading: boolean
}

const STORAGE_KEY = 'aetos.map.displayOptions'

/** Speed on, the rest off. A track's speed is usually the single most operationally relevant
 *  number at a glance; altitude and heading are one click away without being on by default,
 *  which is what keeps the map lean the first time anyone opens it. */
const DEFAULT_OPTIONS: MapDisplayOptions = {
  showSpeed: true,
  showAltitude: false,
  showHeading: false,
}

export function useMapDisplayOptions(): {
  options: MapDisplayOptions
  toggle: (key: keyof MapDisplayOptions) => void
} {
  const [options, setOptions] = useState<MapDisplayOptions>(() => readStored())

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(options))
    } catch {
      // Storage can be unavailable — private browsing, a full quota. Losing persistence
      // across reloads is a minor inconvenience; it must never be the reason the map fails.
    }
  }, [options])

  const toggle = useCallback((key: keyof MapDisplayOptions) => {
    setOptions((current) => ({ ...current, [key]: !current[key] }))
  }, [])

  return { options, toggle }
}

function readStored(): MapDisplayOptions {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_OPTIONS

    const parsed = JSON.parse(raw) as Partial<MapDisplayOptions>
    return {
      showSpeed: parsed.showSpeed ?? DEFAULT_OPTIONS.showSpeed,
      showAltitude: parsed.showAltitude ?? DEFAULT_OPTIONS.showAltitude,
      showHeading: parsed.showHeading ?? DEFAULT_OPTIONS.showHeading,
    }
  } catch {
    // Corrupt or inaccessible storage falls back to defaults rather than breaking the map.
    return DEFAULT_OPTIONS
  }
}
