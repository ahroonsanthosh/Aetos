/**
 * Concentric range rings and their bearing ticks — the PPI's distance scale.
 *
 * Ring spacing is chosen so there are always four or five rings whatever the display range,
 * snapped to a round number of metres. A fixed spacing would give one ring at close range and
 * forty at long range, and a display an operator has to count rings on is not giving them
 * range at a glance. Continuous zoom makes this load-bearing rather than cosmetic: the rings
 * re-step as the operator zooms, so the labels stay numbers you read rather than compute.
 *
 * The bearing *scale* lives in `CompassRose`. This draws only the distance structure and the
 * short ticks that tie the two together.
 */

interface Props {
  /** Display range: sets ring *spacing* and the apparent size of every mark. */
  rangeM: number
  /** Furthest distance from the site that is currently on screen. Rings are drawn out to
   *  here rather than to `rangeM`, so a panned view still has a distance reference under it
   *  instead of unlit, ungridded black. */
  coverageM: number
}

const SPOKE_BEARINGS = [0, 45, 90, 135, 180, 225, 270, 315]

/** Ceiling on rings drawn at once. A far-panned view at close zoom can otherwise ask for
 *  thousands, which is a stalled browser rather than a useful display. */
const MAX_RINGS = 60

export function RangeRings({ rangeM, coverageM }: Props) {
  const step = ringStep(rangeM)
  const rings = ringRadii(step, coverageM)

  return (
    <g className="rings" pointerEvents="none">
      {rings.map((radius) => (
        <circle
          key={radius}
          cx={0}
          cy={0}
          r={radius}
          fill="none"
          stroke={isDecade(radius, step) ? 'var(--grid-line-major)' : 'var(--grid-line)'}
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
export function ringStep(rangeM: number): number {
  const rough = rangeM / 5
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const normalised = rough / magnitude

  if (normalised < 2) return magnitude
  if (normalised < 5) return 2 * magnitude
  return 5 * magnitude
}

/** Ring radii from `step` out to `coverageM`, bounded so a far-panned close zoom cannot ask
 *  for an unbounded number of circles. */
function ringRadii(step: number, coverageM: number): number[] {
  const count = Math.min(MAX_RINGS, Math.max(1, Math.floor(coverageM / step)))
  return Array.from({ length: count }, (_, i) => (i + 1) * step)
}

/** Every fifth ring is emphasised. With rings now extending past the display range, "the
 *  outermost one" stopped being a meaningful thing to highlight — a regular major/minor
 *  rhythm is what actually helps the eye count them. */
function isDecade(radius: number, step: number): boolean {
  return Math.round(radius / step) % 5 === 0
}

function formatRange(metres: number): string {
  return metres >= 1_000 ? `${(metres / 1_000).toFixed(metres % 1_000 === 0 ? 0 : 1)}km` : `${metres}m`
}
