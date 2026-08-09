/**
 * One track on the display: marker, predicted path, label and data block.
 *
 * Everything drawn here comes from backend state, except the predicted path (see
 * `PredictedPath`, which is honest about what it is and is not). Two information levels:
 *
 * - **Unselected** tracks show a lean, single-line data block built from whatever fields
 *   `displayOptions` has toggled on — the point of the toggle is to let an operator decide
 *   how busy the default view gets.
 * - The **selected** track ignores the toggle and shows every field. Clicking a track is
 *   an explicit request for detail, and the toggle exists to manage ambient clutter, not to
 *   hide information from something an operator is actively looking at.
 */

import type { DisplayTrack } from '../../data/models/track'
import { trackColourToken } from '../../data/models/track'
import type { MapDisplayOptions } from './useMapDisplayOptions'
import { PredictedPath } from './PredictedPath'

interface Props {
  track: DisplayTrack
  selected: boolean
  rangeM: number
  displayOptions: MapDisplayOptions
  onSelect: (trackId: string) => void
}

/** Vertical spacing between stacked label lines, in marker units. One place to change the
 *  rhythm of the whole data block rather than a magic number at every call site — the next
 *  field this grows to hold (classification, once that module exists) is one array entry. */
const LINE_HEIGHT_UNITS = 1.5
const FIRST_LINE_OFFSET_UNITS = 1.7

interface Line {
  text: string
  colour: string
}

export function TrackMarker({ track, selected, rangeM, displayOptions, onSelect }: Props) {
  const { eastM, northM } = track.enu
  const colour = trackColourToken(track, selected)
  const stale = track.state === 'STALE'

  // Marker geometry is scaled to the display range so it stays a constant apparent size as
  // the operator zooms, rather than becoming a dot at 20 km and a blob at 500 m.
  const unit = rangeM / 100
  const headingRad = (track.headingDeg * Math.PI) / 180
  const vectorLength = Math.min(track.speedMps * 6, rangeM / 12)
  const vectorX = eastM + Math.sin(headingRad) * vectorLength
  const vectorY = northM + Math.cos(headingRad) * vectorLength

  const lines = buildLines(track, selected, displayOptions, stale)

  // Background block sized from whichever row is actually widest. The ID uses a larger font
  // than the data lines beneath it (1.5 vs 1.15 units), so raw character counts alone would
  // under-size the box whenever the ID happens to be the longest row — the per-row font size
  // is folded in before comparing.
  const idWidthUnits = track.id.length * 1.5
  const widestLineUnits = lines.length ? Math.max(...lines.map((l) => l.text.length * 1.15)) : 0
  const blockWidth = Math.max(idWidthUnits, widestLineUnits) * unit * 0.72

  // The background always covers at least the ID row, and grows to include every stacked
  // line below it. The last stacked line is the southernmost (lowest on screen); padding
  // above the ID and below the last row keeps the background from hugging the text.
  const blockTopY = northM + unit * 0.9
  const lastRowY = lines.length
    ? northM - unit * (FIRST_LINE_OFFSET_UNITS + (lines.length - 1) * LINE_HEIGHT_UNITS)
    : northM
  const blockBottomY = lastRowY - unit * 0.7

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
      <PredictedPath track={track} selected={selected} rangeM={rangeM} />

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
          className="track-marker__selection-ring"
          cx={eastM}
          cy={northM}
          r={unit * 2.2}
          fill="none"
          stroke={colour}
          strokeWidth={unit * 0.12}
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

      {/* Legibility background behind the ID and any stacked lines beneath it, so the label
          reads clearly over range rings and zone fills rather than only over open space.
          Unlike the <text> elements below, a rectangle needs no counter-flip — a mirrored
          rectangle is still a rectangle, only text glyphs render wrong under the parent
          group's y-flip. So this sits directly in the same data space as the marker circle:
          y is the block's screen-bottom edge (the smaller, more-southerly data value), and
          it grows northward (up-screen) by `height` from there. */}
      <rect
        x={eastM + unit * 1.5}
        y={blockBottomY}
        width={blockWidth + unit}
        height={blockTopY - blockBottomY}
        fill="var(--surface-void)"
        opacity={0.55}
        rx={unit * 0.2}
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

      {lines.map((line, index) => {
        const y = northM - unit * (FIRST_LINE_OFFSET_UNITS + index * LINE_HEIGHT_UNITS)
        return (
          <text
            key={line.text}
            x={eastM + unit * 1.8}
            y={y}
            transform={`scale(1, -1) translate(0, ${-2 * y})`}
            fill={line.colour}
            fontSize={unit * 1.15}
            fontFamily="var(--font-mono)"
            dominantBaseline="middle"
          >
            {line.text}
          </text>
        )
      })}
    </g>
  )
}

/** Every secondary label line under the ID, in stacking order. Built as one list rather than
 *  scattered conditional `<text>` elements, so the vertical rhythm (LINE_HEIGHT_UNITS) stays
 *  correct automatically as fields are added or removed. */
function buildLines(
  track: DisplayTrack,
  selected: boolean,
  displayOptions: MapDisplayOptions,
  stale: boolean,
): Line[] {
  const lines: Line[] = []

  if (stale) {
    lines.push({ text: 'STALE', colour: 'var(--status-warning)' })
  } else if (track.coasting) {
    lines.push({ text: 'COASTING', colour: 'var(--status-warning)' })
  }

  if (selected) {
    // Clicking a track is an explicit request for detail — every field shows, regardless of
    // what the ambient display-options toggle is set to.
    lines.push({ text: `SPD ${track.speedMps.toFixed(1)} m/s`, colour: 'var(--text-primary)' })
    lines.push({ text: `ALT ${track.position.altitudeM.toFixed(0)} m`, colour: 'var(--text-primary)' })
    lines.push({ text: `HDG ${track.headingDeg.toFixed(0)}°`, colour: 'var(--text-primary)' })
    return lines
  }

  const fields: string[] = []
  if (displayOptions.showSpeed) fields.push(`${track.speedMps.toFixed(1)} m/s`)
  if (displayOptions.showAltitude) fields.push(`${track.position.altitudeM.toFixed(0)} m`)
  if (displayOptions.showHeading) fields.push(`${track.headingDeg.toFixed(0)}°`)

  if (fields.length > 0) {
    lines.push({ text: fields.join(' · '), colour: 'var(--text-muted)' })
  }

  return lines
}
