/**
 * The bearing scale around the edge of the scope.
 *
 * A plan position indicator is a polar display, and a polar display without a graduated
 * bearing scale asks the operator to estimate angles by eye. This is the ring that makes
 * "bearing 037" something you can read off the display instead of infer from it.
 *
 * Three tiers, coarsest most prominent: cardinals every 90°, numerals every 30°, minor ticks
 * every 10°. That hierarchy is what lets the eye land on the right decade first and refine,
 * rather than counting identical marks.
 *
 * Drawn relative to the *site*, not the viewport centre, because bearings on this display are
 * bearings from the site — that is what the whole coordinate frame means. Panning therefore
 * carries the rose with the site, and it can leave the screen; that is correct, and better
 * than a rose pinned to the window edge that would silently start meaning something else.
 */

interface Props {
  /** Radius the scale sits at, in ENU metres: the largest circle around the site that is
   *  entirely on screen. Shrinks as the operator pans away from the site, so the scale stays
   *  a complete circle rather than an arc running off two edges. */
  radiusM: number
  /** Display range, used only for the apparent size of ticks and numerals. Kept separate from
   *  `radiusM` so panning moves the scale without also shrinking its lettering. */
  scaleM: number
}

const MINOR_STEP_DEG = 10
const NUMERAL_STEP_DEG = 30
const CARDINALS: ReadonlyArray<{ bearing: number; label: string }> = [
  { bearing: 0, label: 'N' },
  { bearing: 90, label: 'E' },
  { bearing: 180, label: 'S' },
  { bearing: 270, label: 'W' },
]

export function CompassRose({ radiusM, scaleM }: Props) {
  const unit = scaleM / 100
  const bearings: number[] = []
  for (let b = 0; b < 360; b += MINOR_STEP_DEG) bearings.push(b)

  return (
    <g className="compass-rose" pointerEvents="none">
      <circle
        cx={0}
        cy={0}
        r={radiusM}
        fill="none"
        stroke="var(--grid-line-major)"
        strokeWidth={unit * 0.12}
      />

      {bearings.map((bearing) => {
        const radians = (bearing * Math.PI) / 180
        const dx = Math.sin(radians)
        const dy = Math.cos(radians)
        const isCardinal = bearing % 90 === 0
        const isNumeral = bearing % NUMERAL_STEP_DEG === 0
        const length = unit * (isCardinal ? 3.2 : isNumeral ? 2.2 : 1.2)

        return (
          <line
            key={bearing}
            x1={dx * radiusM}
            y1={dy * radiusM}
            x2={dx * (radiusM - length)}
            y2={dy * (radiusM - length)}
            stroke={isNumeral ? 'var(--grid-line-major)' : 'var(--grid-line)'}
            strokeWidth={unit * (isNumeral ? 0.18 : 0.1)}
          />
        )
      })}

      {/* Numerals sit inside the ticks. Three digits by convention — 037, not 37 — because a
          fixed width is what makes a column of bearings scannable. */}
      {bearings
        .filter((bearing) => bearing % NUMERAL_STEP_DEG === 0 && bearing % 90 !== 0)
        .map((bearing) => {
          const { x, y } = polar(bearing, radiusM - unit * 6)
          return (
            <text
              key={`numeral-${bearing}`}
              x={x}
              // Counter the parent's y-flip: text inside a mirrored group renders upside down.
              transform={`scale(1, -1) translate(0, ${-2 * y})`}
              y={y}
              fill="var(--grid-label)"
              fontSize={unit * 2.6}
              fontFamily="var(--font-mono)"
              textAnchor="middle"
              dominantBaseline="middle"
            >
              {String(bearing).padStart(3, '0')}
            </text>
          )
        })}

      {CARDINALS.map(({ bearing, label }) => {
        const { x, y } = polar(bearing, radiusM - unit * 6.5)
        return (
          <text
            key={label}
            x={x}
            transform={`scale(1, -1) translate(0, ${-2 * y})`}
            y={y}
            fill="var(--text-secondary)"
            fontSize={unit * 4}
            fontFamily="var(--font-mono)"
            letterSpacing={unit * 0.2}
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

function polar(bearingDeg: number, radiusM: number): { x: number; y: number } {
  const radians = (bearingDeg * Math.PI) / 180
  return { x: Math.sin(radians) * radiusM, y: Math.cos(radians) * radiusM }
}
