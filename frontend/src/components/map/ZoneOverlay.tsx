/**
 * Site zones and the protected area boundary.
 *
 * Zones are drawn as outlines with only a very faint fill. A strongly filled zone would
 * dominate the display, and the tracks are the thing an operator is reading — the zone is the
 * context they are read against.
 *
 * Altitude bounds are not rendered. A plan view has no vertical axis to show them on, and
 * shading a zone differently to hint at a floor or ceiling would imply a spatial meaning the
 * display cannot actually express. They appear in the track details panel instead, where
 * there is room to state them.
 */

import type { SiteView, ZoneKind } from '../../data/models/api'
import type { LocalFrame } from '../../data/models/geo'

interface Props {
  site: SiteView
  frame: LocalFrame
  rangeM: number
}

const ZONE_COLOUR: Record<ZoneKind, string> = {
  protected: 'var(--zone-protected)',
  restricted: 'var(--zone-restricted)',
  monitored: 'var(--zone-monitored)',
}

export function ZoneOverlay({ site, frame, rangeM }: Props) {
  const stroke = rangeM / 700

  return (
    <g className="zones" pointerEvents="none">
      {/* The primary protected radius, drawn first and beneath the configured zones. This is
          the site's headline figure and the distance risk assessment is measured against. */}
      <circle
        cx={0}
        cy={0}
        r={site.protected_radius_m}
        fill="var(--status-critical-dim)"
        stroke="var(--zone-protected)"
        strokeWidth={stroke * 1.5}
        strokeDasharray={`${rangeM / 90} ${rangeM / 180}`}
        opacity={0.75}
      />

      {site.zones.map((zone) => {
        const colour = ZONE_COLOUR[zone.kind]

        if (zone.geometry.shape === 'circle') {
          const centre = frame.toEnu({
            latitude: zone.geometry.centre.latitude,
            longitude: zone.geometry.centre.longitude,
            altitude_m: site.reference.altitude_m,
          })
          return (
            <circle
              key={zone.zone_id}
              cx={centre.eastM}
              cy={centre.northM}
              r={zone.geometry.radius_m}
              fill={colour}
              fillOpacity={0.04}
              stroke={colour}
              strokeWidth={stroke}
              strokeOpacity={0.55}
            />
          )
        }

        const points = zone.geometry.vertices
          .map((vertex) => {
            const enu = frame.toEnu({
              latitude: vertex.latitude,
              longitude: vertex.longitude,
              altitude_m: site.reference.altitude_m,
            })
            return `${enu.eastM},${enu.northM}`
          })
          .join(' ')

        // The backend closes polygon rings implicitly and rejects an explicitly repeated
        // first vertex, so <polygon> — which also closes implicitly — is the exact match.
        return (
          <polygon
            key={zone.zone_id}
            points={points}
            fill={colour}
            fillOpacity={0.04}
            stroke={colour}
            strokeWidth={stroke}
            strokeOpacity={0.55}
          />
        )
      })}

      {/* Site reference: a small cross at the origin of everything. */}
      <g stroke="var(--text-muted)" strokeWidth={rangeM / 900} opacity={0.8}>
        <line x1={-rangeM / 90} y1={0} x2={rangeM / 90} y2={0} />
        <line x1={0} y1={-rangeM / 90} x2={0} y2={rangeM / 90} />
      </g>
    </g>
  )
}
