/**
 * The control for which fields appear in an unselected track's on-map data block.
 *
 * Deliberately styled and positioned like the range selector next to it — both are the same
 * kind of thing: a small, always-available control over how much the display shows, not a
 * settings page an operator has to leave the map to find.
 */

import type { MapDisplayOptions as Options } from './useMapDisplayOptions'

interface Props {
  options: Options
  onToggle: (key: keyof Options) => void
}

const FIELDS: ReadonlyArray<{ key: keyof Options; label: string }> = [
  { key: 'showSpeed', label: 'Speed' },
  { key: 'showAltitude', label: 'Altitude' },
  { key: 'showHeading', label: 'Heading' },
]

export function MapDisplayOptions({ options, onToggle }: Props) {
  return (
    <div className="map__display-options">
      <span className="label">Data block</span>
      <div className="map__display-buttons">
        {FIELDS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            className={`map__display-button ${options[key] ? 'is-active' : ''}`}
            aria-pressed={options[key]}
            onClick={() => onToggle(key)}
            title={`${options[key] ? 'Hide' : 'Show'} ${label.toLowerCase()} on unselected tracks`}
          >
            {label}
          </button>
        ))}
      </div>
    </div>
  )
}
