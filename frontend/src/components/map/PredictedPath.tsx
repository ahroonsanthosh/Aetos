/**
 * The path a track will follow if its current speed and heading are held constant.
 *
 * A straight-line extrapolation, nothing more — it has no notion of turning or acceleration,
 * and the API exposes no positional uncertainty to draw a proper confidence cone from (the
 * backend's Kalman filter has one internally, but it is not part of the wire contract).
 * Drawing a cone anyway, with invented bounds, would imply a calibrated uncertainty that does
 * not exist. The honest choice is a single line, marked as a projection through colour,
 * dashing and — for the selected track — explicit time labels, rather than through a
 * fabricated shape that looks more rigorous than the number behind it actually is.
 *
 * Every displayed track gets a short version, for at-a-glance heading. The selected track
 * gets a longer one with time-tick marks, because that is the one an operator has actually
 * asked "where is this going" about — everyone else stays lean so the display does not fill
 * with speculative lines for tracks nobody is currently looking at.
 */

import type { DisplayTrack } from '../../data/models/track'

interface Props {
  track: DisplayTrack
  selected: boolean
  rangeM: number
}

const SHORT_HORIZON_S = 20
const LONG_HORIZON_S = 60
const TICK_INTERVAL_S = 15

/** Below this speed the filtered heading is mostly jitter — a nearly stationary object's
 *  estimate swings with small position noise, and projecting it forward would draw a
 *  confident-looking line in a direction that means very little. */
const MIN_SPEED_MPS = 0.5

export function PredictedPath({ track, selected, rangeM }: Props) {
  if (track.speedMps < MIN_SPEED_MPS) return null

  const unit = rangeM / 100
  const { eastM, northM } = track.enu
  const headingRad = (track.headingDeg * Math.PI) / 180
  const dx = Math.sin(headingRad)
  const dy = Math.cos(headingRad)

  const horizon = selected ? LONG_HORIZON_S : SHORT_HORIZON_S
  const endDistance = track.speedMps * horizon
  const endX = eastM + dx * endDistance
  const endY = northM + dy * endDistance

  const ticks: number[] = []
  if (selected) {
    for (let t = TICK_INTERVAL_S; t <= horizon; t += TICK_INTERVAL_S) ticks.push(t)
  }

  return (
    <g className="predicted-path" pointerEvents="none">
      <line
        x1={eastM}
        y1={northM}
        x2={endX}
        y2={endY}
        stroke="var(--track-prediction)"
        strokeWidth={unit * (selected ? 0.18 : 0.15)}
        strokeDasharray={`${unit * 0.6} ${unit * 0.6}`}
      />

      {ticks.map((t) => {
        const distance = track.speedMps * t
        const tickX = eastM + dx * distance
        const tickY = northM + dy * distance
        // Perpendicular to the heading, so the hash mark crosses the line rather than
        // running along it.
        const perpX = -dy
        const perpY = dx
        const half = unit * 0.5

        return (
          <g key={t}>
            <line
              x1={tickX - perpX * half}
              y1={tickY - perpY * half}
              x2={tickX + perpX * half}
              y2={tickY + perpY * half}
              stroke="var(--track-prediction)"
              strokeWidth={unit * 0.15}
            />
            <text
              x={tickX + perpX * half * 2.2}
              y={tickY + perpY * half * 2.2}
              transform={`scale(1, -1) translate(0, ${-2 * (tickY + perpY * half * 2.2)})`}
              fill="var(--text-disabled)"
              fontSize={unit * 0.85}
              fontFamily="var(--font-mono)"
              textAnchor="middle"
              dominantBaseline="middle"
            >
              {`+${t}s`}
            </text>
          </g>
        )
      })}
    </g>
  )
}
