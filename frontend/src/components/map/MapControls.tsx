/**
 * Zoom and range chrome.
 *
 * Continuous zoom lives on the wheel; these are the discoverable equivalents. Both exist
 * deliberately — the wheel is how anyone who has used a map will reach for it first, and the
 * buttons are how anyone who has not finds out it is possible at all.
 *
 * The range presets stay because a counter-UAS operator's questions are scale-specific
 * ("what is inside 1 km") in a way free zoom answers badly: getting back to exactly 5 km by
 * wheel is fiddly, and by button is one click.
 */

import { MAX_RANGE_M, MIN_RANGE_M } from './useMapViewport'

interface Props {
  rangeM: number
  isDefault: boolean
  onSetRange: (rangeM: number) => void
  onZoom: (steps: number) => void
  onReset: () => void
}

const PRESETS = [1_000, 2_000, 5_000, 10_000, 20_000]

/** How close a continuous range has to be to a preset for that preset to read as active.
 *  Without a tolerance, wheel-zooming to visually-5 km leaves every button looking inactive,
 *  which makes the row look broken rather than merely unsnapped. */
const PRESET_MATCH_RATIO = 0.02

export function MapControls({ rangeM, isDefault, onSetRange, onZoom, onReset }: Props) {
  return (
    <div className="map__controls">
      <div className="map__zoom">
        <button
          type="button"
          className="map__zoom-button"
          onClick={() => onZoom(1)}
          disabled={rangeM <= MIN_RANGE_M}
          aria-label="Zoom in"
          title="Zoom in (scroll wheel)"
        >
          +
        </button>
        <button
          type="button"
          className="map__zoom-button"
          onClick={() => onZoom(-1)}
          disabled={rangeM >= MAX_RANGE_M}
          aria-label="Zoom out"
          title="Zoom out (scroll wheel)"
        >
          −
        </button>
        <button
          type="button"
          className="map__zoom-button map__zoom-button--reset"
          onClick={onReset}
          disabled={isDefault}
          aria-label="Recentre on site"
          title="Recentre on site (double-click the map)"
        >
          ⌂
        </button>
      </div>

      <div className="map__scale-buttons">
        {PRESETS.map((option) => (
          <button
            key={option}
            type="button"
            className={`map__scale-button ${matches(rangeM, option) ? 'is-active' : ''}`}
            onClick={() => onSetRange(option)}
          >
            {option >= 1_000 ? `${option / 1_000}km` : `${option}m`}
          </button>
        ))}
      </div>

      <span className="map__range-readout numeric" aria-live="off">
        {formatRange(rangeM)}
      </span>
    </div>
  )
}

function matches(rangeM: number, preset: number): boolean {
  return Math.abs(rangeM - preset) / preset < PRESET_MATCH_RATIO
}

export function formatRange(metres: number): string {
  if (metres >= 10_000) return `${(metres / 1_000).toFixed(0)} km`
  if (metres >= 1_000) return `${(metres / 1_000).toFixed(1)} km`
  return `${metres.toFixed(0)} m`
}
