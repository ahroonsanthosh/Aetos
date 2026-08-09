/**
 * The operational map: a site-centred plan position indicator.
 *
 * Rendered as SVG, in a viewBox measured in metres of the site's local ENU frame, so track
 * positions are plotted in the same units the backend computes them in and there is no scale
 * arithmetic scattered through the child components. East is +x, north is +y, and the y axis
 * is flipped once here — at the transform — because screen coordinates grow downwards and
 * north does not.
 *
 * See ADR 0010 for why this is a PPI rather than a geographic basemap. In short: counter-UAS
 * operation is site-centric, the backend already works in this frame, and a display with no
 * external tile dependency renders identically offline — which matters for a system whose
 * replay is the primary demonstration mechanism.
 *
 * Selecting a track is what unlocks its detail on the map: a history trail (fetched here,
 * only for the selected track), an extended predicted path, and a full data block instead of
 * the lean toggleable one everyone else gets. Deselecting reverses all three immediately —
 * the trail fetch stops, the trail itself stops rendering, and the previously-selected
 * marker reverts to the same lean presentation as its neighbours. Nothing has to be told to
 * "declutter"; it falls out of `selected` going back to `false`.
 */

import { useMemo, useState } from 'react'

import type { SensorView, SiteView } from '../../data/models/api'
import type { LocalFrame } from '../../data/models/geo'
import type { DisplayTrack } from '../../data/models/track'
import { useTrackHistory } from '../../data/client/useTrackHistory'
import { MapDisplayOptions } from './MapDisplayOptions'
import { RangeRings } from './RangeRings'
import { SensorMarkers } from './SensorMarkers'
import { TrackMarker } from './TrackMarker'
import { TrackTrail } from './TrackTrail'
import { useMapDisplayOptions } from './useMapDisplayOptions'
import { ZoneOverlay } from './ZoneOverlay'
import './OperationalMap.css'

interface Props {
  site: SiteView | null
  frame: LocalFrame | null
  sensors: readonly SensorView[]
  tracks: readonly DisplayTrack[]
  selectedTrackId: string | null
  onSelectTrack: (trackId: string | null) => void
}

/** Range span the display covers, in metres from the site reference. */
const DEFAULT_RANGE_M = 5_000
const MIN_RANGE_M = 500
const MAX_RANGE_M = 40_000

export function OperationalMap({
  site,
  frame,
  sensors,
  tracks,
  selectedTrackId,
  onSelectTrack,
}: Props) {
  const [rangeM, setRangeM] = useState(DEFAULT_RANGE_M)
  const { options: displayOptions, toggle: toggleDisplayOption } = useMapDisplayOptions()
  const history = useTrackHistory(selectedTrackId, frame)

  const visible = useMemo(
    // The backend decides what belongs on an operator's map, not the client. A single
    // unconfirmed return is what clutter looks like, and a client applying its own rule would
    // eventually apply a different one than the backend's own tests assert.
    () => tracks.filter((t) => t.displayable),
    [tracks],
  )

  if (!site || !frame) {
    return (
      <div className="map map--empty">
        <span className="label">Awaiting site configuration</span>
      </div>
    )
  }

  return (
    <div className="map">
      <svg
        className="map__canvas"
        viewBox={`${-rangeM} ${-rangeM} ${rangeM * 2} ${rangeM * 2}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={`Operational picture for ${site.name}, ${visible.length} tracks displayed`}
        onClick={() => onSelectTrack(null)}
      >
        {/* One flip, here. Everything below draws in ENU metres with north up. */}
        <g transform="scale(1, -1)">
          <ZoneOverlay site={site} frame={frame} rangeM={rangeM} />
          <RangeRings rangeM={rangeM} />
          <SensorMarkers sensors={sensors} frame={frame} rangeM={rangeM} />

          {/* Drawn beneath the markers, so a track sitting on top of its own trail's start
              is never obscured by it. Only ever has points when a track is selected. */}
          {history.trackId === selectedTrackId && (
            <TrackTrail points={history.points} rangeM={rangeM} />
          )}

          {visible.map((track) => (
            <TrackMarker
              key={track.id}
              track={track}
              selected={track.id === selectedTrackId}
              rangeM={rangeM}
              displayOptions={displayOptions}
              onSelect={(id) => onSelectTrack(id)}
            />
          ))}
        </g>
      </svg>

      <div className="map__scale">
        <span className="label">Range</span>
        <div className="map__scale-buttons">
          {[1_000, 2_000, 5_000, 10_000, 20_000].map((option) => (
            <button
              key={option}
              type="button"
              className={`map__scale-button ${option === rangeM ? 'is-active' : ''}`}
              onClick={() => setRangeM(clamp(option))}
            >
              {option >= 1_000 ? `${option / 1_000}km` : `${option}m`}
            </button>
          ))}
        </div>
      </div>

      <MapDisplayOptions options={displayOptions} onToggle={toggleDisplayOption} />

      <div className="map__legend">
        <span className="label">{site.name}</span>
        <span className="label numeric">
          {site.reference.latitude.toFixed(4)}, {site.reference.longitude.toFixed(4)}
        </span>
        <span className="label">
          {visible.length} displayed / {tracks.length} tracked
        </span>
        {selectedTrackId && history.error && (
          <span className="label map__legend-warning">History unavailable</span>
        )}
      </div>
    </div>
  )
}

function clamp(value: number): number {
  return Math.min(MAX_RANGE_M, Math.max(MIN_RANGE_M, value))
}
