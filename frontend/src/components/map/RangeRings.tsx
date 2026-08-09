/**
 * Concentric range rings, bearing spokes and cardinal marks — the PPI's coordinate system.
 *
 * Ring spacing is chosen so there are always four or five rings whatever the display range.
 * A fixed spacing would give one ring at close range and forty at long range, and a display
 * an operator has to count rings on is not giving them range at a glance.
 */

interface Props {
  rangeM: number
}

const SPOKE_BEARINGS = [0, 45, 90, 135, 180, 225, 270, 315]
const CARDINALS: ReadonlyArray<{ bearing: number; label: string }> = [
  { bearing: 0, label: 'N' },
  { bearing: 90, label: 'E' },
  { bearing: 180, label: 'S' },
  { bearing: 270, label: 'W' },
]

export function RangeRings({ rangeM }: Props) {
  const step = ringStep(rangeM)
  const rings: number[] = []
  for (let r = step; r <= rangeM; r += step) rings.push(r)

  return (
    <g className="rings" pointerEvents="none">
      {rings.map((radius) => (
        <circle
          key={radius}
          cx={0}
          cy={0}
          r={radius}
          fill="none"
          stroke={radius === rangeM ? 'var(--grid-line-major)' : 'var(--grid-line)'}
          strokeWidth={rangeM / 800}
        />
      ))}

      {/* Bearing marks are ticks that cross each ring, not full-length radials from the
          centre.

          A radial spoke and an inbound track's trail are the same shape — a long straight
          line pointing at the site — and an object approaching on a bearing near one of
          these (an aircraft at 314° against the 315° spoke) laid the two almost on top of
          each other, converging at the centre into a long thin wedge that read as a drawn
          feature of the track. Decoration must never be mistakable for data.

          Ticks give the same bearing reference where it is actually used — against a ring,
          where range and bearing are read together — and cannot be confused with a path. */}
      {SPOKE_BEARINGS.map((bearing) => {
        const radians = (bearing * Math.PI) / 180
        const dx = Math.sin(radians)
        const dy = Math.cos(radians)
        const half = rangeM / 90

        return (
          <g key={bearing}>
            {rings.map((radius) => (
              <line
                key={radius}
                x1={dx * (radius - half)}
                y1={dy * (radius - half)}
                x2={dx * (radius + half)}
                y2={dy * (radius + half)}
                stroke="var(--grid-line)"
                strokeWidth={rangeM / 1000}
              />
            ))}
          </g>
        )
      })}

      {/* Ring labels sit on the north-east diagonal rather than on a spoke, so they do not
          collide with the cardinal letters. */}
      {rings.map((radius) => (
        <text
          key={`label-${radius}`}
          x={radius * Math.SQRT1_2}
          y={-radius * Math.SQRT1_2}
          // Counter the parent's y-flip: text drawn inside a mirrored group renders upside
          // down, and a mirrored numeral is worse than no numeral.
          transform={`scale(1, -1) translate(0, ${-2 * radius * Math.SQRT1_2})`}
          fill="var(--grid-label)"
          fontSize={rangeM / 55}
          fontFamily="var(--font-mono)"
          textAnchor="middle"
        >
          {formatRange(radius)}
        </text>
      ))}

      {CARDINALS.map(({ bearing, label }) => {
        const radians = (bearing * Math.PI) / 180
        const x = Math.sin(radians) * rangeM * 0.94
        const y = Math.cos(radians) * rangeM * 0.94
        return (
          <text
            key={label}
            x={x}
            y={y}
            transform={`scale(1, -1) translate(0, ${-2 * y})`}
            fill="var(--text-muted)"
            fontSize={rangeM / 40}
            fontFamily="var(--font-mono)"
            textAnchor="middle"
            dominantBaseline="middle"
          >
            {label}
          </text>
        )
      })}
    </g>
  )
}

/**
 * Ring spacing that keeps four or five rings on screen at any range.
 *
 * Snapped to 1, 2 or 5 times a power of ten so ring labels are numbers an operator reads
 * rather than computes. The thresholds round *down* to the next nice number rather than to
 * the nearest: rounding up gives too few rings, and at a 5 km range the difference is two
 * rings versus five.
 */
function ringStep(rangeM: number): number {
  const rough = rangeM / 5
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const normalised = rough / magnitude

  if (normalised < 2) return magnitude
  if (normalised < 5) return 2 * magnitude
  return 5 * magnitude
}

function formatRange(metres: number): string {
  return metres >= 1_000 ? `${(metres / 1_000).toFixed(metres % 1_000 === 0 ? 0 : 1)}km` : `${metres}m`
}
