/**
 * Sensor positions on the display.
 *
 * Position and boresight only. Coverage arcs are deliberately absent: the detection range and
 * field of view that would define them live in the sensor registry, which the API does not
 * expose, and drawing a plausible-looking coverage wedge from a guessed range is exactly the
 * fabrication ADR 0011 forbids. The arcs go in when the backend can say what they are.
 *
 * A disabled sensor is drawn, dimmed, rather than omitted — an operator needs to be able to
 * tell "this site has no sensor there" from "the sensor there is switched off".
 */

import type { SensorView } from '../../data/models/api'
import type { LocalFrame } from '../../data/models/geo'

interface Props {
  sensors: readonly SensorView[]
  frame: LocalFrame
  rangeM: number
}

export function SensorMarkers({ sensors, frame, rangeM }: Props) {
  const unit = rangeM / 100

  return (
    <g className="sensors" pointerEvents="none">
      {sensors.map((sensor) => {
        const enu = frame.toEnu({
          latitude: sensor.position.latitude,
          longitude: sensor.position.longitude,
          altitude_m: sensor.height_m,
        })
        const colour = sensor.enabled ? 'var(--sensor-marker)' : 'var(--text-disabled)'
        const half = unit * 1.1

        return (
          <g key={sensor.sensor_id}>
            {/* Square, to be unmistakable against the circular track markers. Shape carries
                the distinction rather than colour alone, which keeps it legible for an
                operator who cannot rely on hue. */}
            <rect
              x={enu.eastM - half}
              y={enu.northM - half}
              width={half * 2}
              height={half * 2}
              fill="none"
              stroke={colour}
              strokeWidth={unit * 0.22}
            />

            {/* Boresight tick. Meaningless for a sensor with full azimuthal coverage, but the
                API does not say which those are, so it is drawn short and faint rather than
                asserted as a coverage direction. */}
            <line
              x1={enu.eastM}
              y1={enu.northM}
              x2={enu.eastM + Math.sin((sensor.boresight_deg * Math.PI) / 180) * unit * 2.4}
              y2={enu.northM + Math.cos((sensor.boresight_deg * Math.PI) / 180) * unit * 2.4}
              stroke={colour}
              strokeWidth={unit * 0.12}
              opacity={0.55}
            />

            <text
              x={enu.eastM}
              y={enu.northM - unit * 2}
              transform={`scale(1, -1) translate(0, ${-2 * (enu.northM - unit * 2)})`}
              fill={colour}
              fontSize={unit * 1.2}
              fontFamily="var(--font-mono)"
              textAnchor="middle"
              opacity={sensor.enabled ? 0.85 : 0.5}
            >
              {sensor.sensor_id}
            </text>
          </g>
        )
      })}
    </g>
  )
}
