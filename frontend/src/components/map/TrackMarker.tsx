/**
 * One track on the display: marker, heading vector, predicted path and label.
 *
 * Everything drawn here comes from backend state. The predicted path is the only extrapolation
 * and it is drawn as a dashed line, visually distinct from the solid heading vector, so an
 * operator can tell an observation from a projection at a glance — the same distinction the
 * backend draws between a detection and a coasted position.
 */

import type { DisplayTrack } from '../../data/models/track'
import { trackColourToken } from '../../data/models/track'

interface Props {
  track: DisplayTrack
  selected: boolean
  rangeM: number
  onSelect: (trackId: string) => void
}

/** How far ahead the predicted path is drawn. */
const PREDICTION_HORIZON_S = 20

export function TrackMarker({ track, selected, rangeM, onSelect }: Props) {
  const { eastM, northM } = track.enu
  const colour = trackColourToken(track, selected)

  // Marker geometry is scaled to the display range so it stays a constant apparent size as
  // the operator zooms, rather than becoming a dot at 20 km and a blob at 500 m.
  const unit = rangeM / 100
  const headingRad = (track.headingDeg * Math.PI) / 180

  const vectorLength = Math.min(track.speedMps * 6, rangeM / 12)
  const vectorX = eastM + Math.sin(headingRad) * vectorLength
  const vectorY = northM + Math.cos(headingRad) * vectorLength

  const predictionDistance = track.speedMps * PREDICTION_HORIZON_S
  const predictionX = eastM + Math.sin(headingRad) * predictionDistance
  const predictionY = northM + Math.cos(headingRad) * predictionDistance

  const stale = track.state === 'STALE'

  return (
    <g
      className="track-marker"
      onClick={(clickEvent) => {
        // The map's own click clears the selection. Without this the two handlers fight and
        // selecting a track immediately deselects it.
        clickEvent.stopPropagation()
        onSelect(track.id)
      }}
      style={{ cursor: 'pointer' }}
    >
      {/* Predicted path, drawn first so the marker sits above it. Dashed and dim: this is
          where the track is going if nothing changes, not where anything has been seen. */}
      {track.speedMps > 0.5 && (
        <line
          x1={eastM}
          y1={northM}
          x2={predictionX}
          y2={predictionY}
          stroke="var(--track-prediction)"
          strokeWidth={unit * 0.15}
          strokeDasharray={`${unit * 0.6} ${unit * 0.6}`}
        />
      )}

      {track.speedMps > 0.5 && (
        <line
          x1={eastM}
          y1={northM}
          x2={vectorX}
          y2={vectorY}
          stroke={colour}
          strokeWidth={unit * 0.2}
        />
      )}

      {selected && (
        <circle
          cx={eastM}
          cy={northM}
          r={unit * 2.2}
          fill="none"
          stroke={colour}
          strokeWidth={unit * 0.12}
          opacity={0.7}
        />
      )}

      {/* A coasting track is drawn hollow. Its position is predicted rather than observed, and
          a filled marker would assert an observation that did not happen. */}
      <circle
        cx={eastM}
        cy={northM}
        r={unit}
        fill={stale || track.coasting ? 'none' : colour}
        stroke={colour}
        strokeWidth={unit * 0.25}
        strokeDasharray={stale ? `${unit * 0.5} ${unit * 0.35}` : undefined}
      />

      <text
        x={eastM + unit * 1.8}
        y={northM}
        transform={`scale(1, -1) translate(0, ${-2 * northM})`}
        fill={selected ? 'var(--track-selected)' : 'var(--text-secondary)'}
        fontSize={unit * 1.5}
        fontFamily="var(--font-mono)"
        dominantBaseline="middle"
      >
        {track.id}
      </text>

      {(stale || track.coasting) && (
        <text
          x={eastM + unit * 1.8}
          y={northM - unit * 1.7}
          transform={`scale(1, -1) translate(0, ${-2 * (northM - unit * 1.7)})`}
          fill="var(--track-stale)"
          fontSize={unit * 1.1}
          fontFamily="var(--font-mono)"
          dominantBaseline="middle"
        >
          {stale ? 'STALE' : 'COASTING'}
        </text>
      )}
    </g>
  )
}
