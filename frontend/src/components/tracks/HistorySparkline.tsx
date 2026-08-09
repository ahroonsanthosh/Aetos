/**
 * A minimal inline chart: one value over time, drawn as a plain SVG polyline.
 *
 * No charting library — a session's worth of one track's history is few enough points that
 * hand-drawn SVG is simpler and lighter than a dependency, and it matches how every other
 * visual element in this interface is built (see `components/map`).
 *
 * Positioned by elapsed time, not by index. History is one point per recorded event, not a
 * fixed-interval sample, so spacing points evenly by index would misrepresent a track that
 * went quiet for a while as one that was changing quickly throughout.
 */

interface Point {
  timestamp: Date
  value: number
}

interface Props {
  points: readonly Point[]
  /** Unit suffix for the axis labels, e.g. 'm' or 'm/s'. */
  unit: string
}

const WIDTH = 600
const HEIGHT = 120
const PADDING = 28

export function HistorySparkline({ points, unit }: Props) {
  const first = points[0]
  const last = points[points.length - 1]

  if (points.length < 2 || !first || !last) {
    return (
      <div className="sparkline sparkline--empty">
        <span className="label">Not enough data</span>
      </div>
    )
  }

  const startMs = first.timestamp.getTime()
  const durationMs = Math.max(1, last.timestamp.getTime() - startMs)

  const values = points.map((p) => p.value)
  const minValue = Math.min(...values)
  const maxValue = Math.max(...values)
  const valueRange = Math.max(1e-6, maxValue - minValue)

  const toX = (t: number) => PADDING + ((t - startMs) / durationMs) * (WIDTH - PADDING * 2)
  const toY = (v: number) =>
    HEIGHT - PADDING - ((v - minValue) / valueRange) * (HEIGHT - PADDING * 2)

  const path = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${toX(p.timestamp.getTime())} ${toY(p.value)}`)
    .join(' ')

  return (
    <svg className="sparkline" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none">
      <line
        x1={PADDING}
        y1={HEIGHT - PADDING}
        x2={WIDTH - PADDING}
        y2={HEIGHT - PADDING}
        stroke="var(--line-subtle)"
      />
      <path d={path} fill="none" stroke="var(--status-info)" strokeWidth={1.5} />
      <text
        x={4}
        y={PADDING - 4}
        fill="var(--text-disabled)"
        fontSize={10}
        fontFamily="var(--font-mono)"
      >
        {maxValue.toFixed(1)}
        {unit}
      </text>
      <text
        x={4}
        y={HEIGHT - PADDING + 12}
        fill="var(--text-disabled)"
        fontSize={10}
        fontFamily="var(--font-mono)"
      >
        {minValue.toFixed(1)}
        {unit}
      </text>
    </svg>
  )
}
