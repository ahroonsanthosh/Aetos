/**
 * The route a selected track has actually taken, drawn from its recorded history.
 *
 * Only ever rendered for the selected track. Fetching and drawing every track's full history
 * at once would be exactly the clutter the click-to-inspect interaction exists to avoid —
 * this is what makes deselecting a track genuinely declutter the display rather than just
 * hide a details panel while the map stays busy underneath.
 *
 * The line fades from dim (oldest) to bright (most recent), so recency reads directly from
 * the trail without a legend. Lifecycle transitions — confirmed, gone stale, lost — get a
 * small marker of their own: `event_type` on each history point is real recorded data, not
 * an embellishment, and knowing *where* a track was first confirmed or first went quiet is
 * genuinely useful information the event log already has.
 */

import type { TrackHistoryPoint } from '../../data/models/track'

interface Props {
  points: readonly TrackHistoryPoint[]
  rangeM: number
}

/** State transitions worth marking. Deliberately excludes TRACK_UPDATED and TRACK_COASTING —
 *  both are "same state, new position" continuations, and a track that coasts for a while
 *  emits one every cycle, which would bury the transitions that actually matter. */
const LIFECYCLE_EVENTS = new Set(['TRACK_CREATED', 'TRACK_CONFIRMED', 'TRACK_STALE', 'TRACK_LOST'])

/** History is one point per recorded event, not a fixed-interval sample, so a track that has
 *  coasted for a long time can accumulate far more points than the display needs to draw a
 *  legible route. Above this, the line is subsampled for rendering — the fetch itself stays
 *  unbounded (see useTrackHistory), only the drawing is capped. Lifecycle markers are never
 *  subsampled, since there are always few of them and they are the points most worth keeping. */
const MAX_LINE_POINTS = 400

export function TrackTrail({ points, rangeM }: Props) {
  if (points.length < 2) return null

  const unit = rangeM / 100
  const line = subsample(points, MAX_LINE_POINTS)
  const lastSegment = line.length - 2

  return (
    <g className="track-trail" pointerEvents="none">
      {line.slice(1).map((point, i) => {
        const previous = line[i]
        if (!previous) return null // unreachable — i is always within line's bounds here

        const recency = lastSegment <= 0 ? 1 : i / lastSegment
        return (
          <line
            key={point.seq}
            x1={previous.enu.eastM}
            y1={previous.enu.northM}
            x2={point.enu.eastM}
            y2={point.enu.northM}
            stroke="var(--track-selected)"
            strokeOpacity={0.18 + recency * 0.65}
            strokeWidth={unit * 0.3}
            strokeLinecap="round"
          />
        )
      })}

      {points
        .filter((point) => LIFECYCLE_EVENTS.has(point.eventType))
        .map((point) => (
          <circle
            key={`marker-${point.seq}`}
            cx={point.enu.eastM}
            cy={point.enu.northM}
            r={unit * 0.45}
            fill="var(--surface-void)"
            stroke="var(--track-selected)"
            strokeWidth={unit * 0.18}
            opacity={0.9}
          >
            <title>{`${point.eventType.replace('TRACK_', '')} at ${point.timestamp.toLocaleTimeString()}`}</title>
          </circle>
        ))}
    </g>
  )
}

/** Evenly-spaced subsample that always keeps the first and last point, so a truncated trail
 *  still starts and ends exactly where the real one does. Exported for direct unit testing. */
export function subsample(points: readonly TrackHistoryPoint[], max: number): TrackHistoryPoint[] {
  if (points.length <= max) return [...points]

  const stride = (points.length - 1) / (max - 1)
  const sampled: TrackHistoryPoint[] = []
  for (let i = 0; i < max; i++) {
    const point = points[Math.round(i * stride)]
    if (point) sampled.push(point) // always true in range, but noUncheckedIndexedAccess needs the guard
  }
  return sampled
}
