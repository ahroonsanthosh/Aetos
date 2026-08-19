/**
 * The operational map: a site-centred plan position indicator.
 *
 * Rendered as SVG, in a viewBox measured in metres of the site's local ENU frame, so track
 * positions are plotted in the same units the backend computes them in and there is no scale
 * arithmetic scattered through the child components. East is +x, north is +y, and the y axis
 * is flipped once here — at the transform — because screen coordinates grow downwards and
 * north does not.
 *
 * See ADR 0010 for why this is a PPI rather than a geographic basemap. In short: counter-UAS
 * operation is site-centric, the backend already works in this frame, and a display with no
 * external tile dependency renders identically offline — which matters for a system whose
 * replay is the primary demonstration mechanism.
 *
 * **Basemap.** An optional geographic layer can be shown beneath, following this viewport and
 * never driving it (ADR 0014). Off by default, and unmounted when off — so the default display
 * has no tile dependency and does not even load the map library.
 *
 * **Navigation.** Scroll to zoom about the cursor, drag to pan, double-click to recentre on
 * the site. The viewport is `useMapViewport`; the screen-to-world conversion is the browser's
 * own `getScreenCTM`, taken from the flipped group, so it stays exact under any container
 * size, aspect ratio or device pixel ratio rather than being re-derived by hand here.
 *
 * Selecting a track is what unlocks its detail on the map: a history trail (fetched here,
 * only for the selected track), an extended predicted path, and a full data block instead of
 * the lean toggleable one everyone else gets. Deselecting reverses all three immediately.
 * Nothing has to be told to "declutter"; it falls out of `selected` going back to `false`.
 */

import { useCallback, useMemo, useRef, useState } from 'react'

import type { SensorView, SiteView } from '../../data/models/api'
import type { LocalFrame } from '../../data/models/geo'
import type { DisplayTrack } from '../../data/models/track'
import { useTrackHistory } from '../../data/client/useTrackHistory'
import { BasemapLayer } from './BasemapLayer'
import { CompassRose } from './CompassRose'
import { MapControls } from './MapControls'
import { MapDisplayOptions } from './MapDisplayOptions'
import { RangeRings } from './RangeRings'
import { SensorMarkers } from './SensorMarkers'
import { TrackMarker } from './TrackMarker'
import { TrackTrail } from './TrackTrail'
import { BASEMAP_FONTS_URL, BASEMAP_TILES_URL, useBasemap } from './useBasemap'
import { useMapDisplayOptions } from './useMapDisplayOptions'
import type { EnuPoint2D } from './useMapViewport'
import {
  fullyVisibleRadiusM,
  gridCoverageM,
  useMapViewport,
  viewBoxFor,
} from './useMapViewport'
import { ZoneOverlay } from './ZoneOverlay'
import './OperationalMap.css'

interface Props {
  site: SiteView | null
  frame: LocalFrame | null
  sensors: readonly SensorView[]
  tracks: readonly DisplayTrack[]
  selectedTrackId: string | null
  onSelectTrack: (trackId: string | null) => void
}

/** Pointer travel, in pixels, past which a press is a drag rather than a click. Below this a
 *  click that wobbles a pixel or two would deselect the track the operator just selected. */
const DRAG_THRESHOLD_PX = 4

export function OperationalMap({
  site,
  frame,
  sensors,
  tracks,
  selectedTrackId,
  onSelectTrack,
}: Props) {
  const { viewport, setRange, zoomAt, panBy, reset } = useMapViewport()
  const { options: displayOptions, toggle: toggleDisplayOption } = useMapDisplayOptions()
  const basemap = useBasemap()
  const history = useTrackHistory(selectedTrackId, frame)

  const worldRef = useRef<SVGGElement | null>(null)
  const [cursor, setCursor] = useState<EnuPoint2D | null>(null)
  const [panning, setPanning] = useState(false)
  const drag = useRef<{ pointerId: number; last: EnuPoint2D; travelPx: number } | null>(null)
  const suppressNextClick = useRef(false)

  const visible = useMemo(
    // The backend decides what belongs on an operator's map, not the client. A single
    // unconfirmed return is what clutter looks like, and a client applying its own rule would
    // eventually apply a different one than the backend's own tests assert.
    () => tracks.filter((t) => t.displayable),
    [tracks],
  )

  /** Screen pixel to ENU metres, via the browser's own transform for the flipped group. */
  const toEnu = useCallback((clientX: number, clientY: number): EnuPoint2D | null => {
    const world = worldRef.current
    const owner = world?.ownerSVGElement
    if (!world || !owner) return null

    const matrix = world.getScreenCTM()
    if (!matrix) return null

    const point = owner.createSVGPoint()
    point.x = clientX
    point.y = clientY
    const local = point.matrixTransform(matrix.inverse())
    return { eastM: local.x, northM: local.y }
  }, [])

  const handleWheel = useCallback(
    (event: React.WheelEvent<SVGSVGElement>) => {
      // deltaY is negative scrolling up, which everywhere else in computing means "towards",
      // so it maps to zooming in.
      const steps = -Math.sign(event.deltaY)
      if (steps !== 0) zoomAt(steps, toEnu(event.clientX, event.clientY))
    },
    [zoomAt, toEnu],
  )

  const handlePointerDown = useCallback(
    (event: React.PointerEvent<SVGSVGElement>) => {
      if (event.button !== 0) return
      const start = toEnu(event.clientX, event.clientY)
      if (!start) return

      drag.current = { pointerId: event.pointerId, last: start, travelPx: 0 }
      // Capture so a drag that leaves the map keeps panning instead of sticking mid-gesture.
      event.currentTarget.setPointerCapture(event.pointerId)
    },
    [toEnu],
  )

  const handlePointerMove = useCallback(
    (event: React.PointerEvent<SVGSVGElement>) => {
      const position = toEnu(event.clientX, event.clientY)
      const active = drag.current

      if (!active || active.pointerId !== event.pointerId) {
        setCursor(position)
        return
      }

      active.travelPx += Math.abs(event.movementX) + Math.abs(event.movementY)
      if (active.travelPx < DRAG_THRESHOLD_PX) return

      setPanning(true)
      if (!position) return

      // Drag the world under the pointer: the viewport moves opposite to the grab.
      panBy(active.last.eastM - position.eastM, active.last.northM - position.northM)

      // Re-read after panning rather than storing `position`: the transform has changed, so
      // the pointer now sits over different world coordinates, and reusing the stale value
      // would make the map accelerate away under a steady drag.
      const settled = toEnu(event.clientX, event.clientY)
      if (settled) active.last = settled
    },
    [panBy, toEnu],
  )

  const endDrag = useCallback((event: React.PointerEvent<SVGSVGElement>) => {
    const active = drag.current
    drag.current = null
    setPanning(false)

    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    // A pan ends with a click event the browser fires anyway. Left alone it would land on the
    // background and clear the operator's selection, so a drag that happened to finish over
    // empty space would silently deselect the track they were inspecting.
    if (active && active.travelPx >= DRAG_THRESHOLD_PX) suppressNextClick.current = true
  }, [])

  const handleClick = useCallback(() => {
    if (suppressNextClick.current) {
      suppressNextClick.current = false
      return
    }
    // Track markers stop propagation, so anything reaching here is background.
    onSelectTrack(null)
  }, [onSelectTrack])

  if (!site || !frame) {
    return (
      <div className="map map--empty">
        <span className="label">Awaiting site configuration</span>
      </div>
    )
  }

  const { rangeM } = viewport
  // The grid follows the view. Both are distances from the *site*, which is what the rings
  // measure — the viewport only decides how much of that has to be drawn.
  const coverageM = gridCoverageM(viewport)
  const roseRadiusM = fullyVisibleRadiusM(viewport)
  const showingBasemap = basemap.enabled && basemap.status === 'ready'

  return (
    <div className="map">
      {/* Beneath the SVG, following its viewport and never driving it. Unmounted entirely when
          off, which is also what unloads MapLibre. */}
      {basemap.enabled && (
        <BasemapLayer
          viewport={viewport}
          frame={frame}
          tilesUrl={BASEMAP_TILES_URL}
          fontsUrl={BASEMAP_FONTS_URL}
          onStatus={basemap.report}
        />
      )}

      <svg
        className={`map__canvas ${panning ? 'is-panning' : ''}`}
        viewBox={viewBoxFor(viewport)}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={`Operational picture for ${site.name}, ${visible.length} tracks displayed`}
        onWheel={handleWheel}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onClick={handleClick}
        onPointerLeave={() => setCursor(null)}
        onDoubleClick={reset}
      >
        <defs>
          {/* Depth, not decoration: the scope face reads as a lit surface with a dark rim,
              which separates "inside the display's range" from "outside it" at a glance —
              a distinction the flat background left entirely to the outer ring. */}
          <radialGradient id="map-scope-face" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--scope-face-inner)" />
            <stop offset="70%" stopColor="var(--scope-face-mid)" />
            <stop offset="100%" stopColor="var(--scope-face-outer)" />
          </radialGradient>
        </defs>

        {/* One flip, here. Everything below draws in ENU metres with north up. */}
        <g transform="scale(1, -1)" ref={worldRef}>
          {/* The lit scope face is the display's own ground. With a basemap under it, it
              would be the thing hiding the terrain the operator just asked for, so it drops to
              a wash that keeps the rings legible without obscuring what is beneath. */}
          <circle
            cx={0}
            cy={0}
            r={coverageM}
            fill="url(#map-scope-face)"
            opacity={showingBasemap ? 0.25 : 1}
          />

          <ZoneOverlay site={site} frame={frame} rangeM={rangeM} />
          <RangeRings rangeM={rangeM} coverageM={coverageM} />
          {/* Only when a full circle fits on screen — see `fullyVisibleRadiusM`. */}
          {roseRadiusM > 0 && <CompassRose radiusM={roseRadiusM} scaleM={rangeM} />}
          <SensorMarkers sensors={sensors} frame={frame} rangeM={rangeM} />

          {/* Drawn beneath the markers, so a track sitting on top of its own trail's start
              is never obscured by it. Only ever has points when a track is selected. */}
          {history.trackId === selectedTrackId && (
            <TrackTrail points={history.points} rangeM={rangeM} />
          )}

          {visible.map((track) => (
            <TrackMarker
              key={track.id}
              track={track}
              selected={track.id === selectedTrackId}
              rangeM={rangeM}
              displayOptions={displayOptions}
              onSelect={(id) => onSelectTrack(id)}
            />
          ))}
        </g>
      </svg>

      <MapControls
        rangeM={rangeM}
        isDefault={viewport.isDefault}
        onSetRange={setRange}
        onZoom={(steps) => zoomAt(steps, null)}
        onReset={reset}
        basemap={basemap}
      />

      <MapDisplayOptions options={displayOptions} onToggle={toggleDisplayOption} />

      <div className="map__legend">
        <span className="label">{site.name}</span>
        <span className="label numeric">
          {site.reference.latitude.toFixed(4)}, {site.reference.longitude.toFixed(4)}
        </span>
        <span className="label">
          {visible.length} displayed / {tracks.length} tracked
        </span>
        {selectedTrackId && history.error && (
          <span className="label map__legend-warning">History unavailable</span>
        )}
        {/* A truncated route is drawn exactly like a complete one, so the difference has to be
            stated. Without this an operator reading the trail would take the most recent
            10,000 points for the whole of where the object has been. */}
        {selectedTrackId && !history.error && history.truncated && (
          <span className="label map__legend-warning">
            Trail truncated — showing latest {history.points.length} of {history.total}
          </span>
        )}
      </div>

      {/* Range and bearing under the pointer. Cheap to provide, and it turns the display from
          something an operator reads into something they can measure with — "how far is that
          from the fence" stops needing a second tool. Site-relative, like every other figure
          on this display. */}
      <div className={`map__cursor ${cursor ? 'is-live' : ''}`}>
        <span className="label">Cursor</span>
        <span className="numeric">{cursor ? formatBearing(cursor) : '—'}</span>
        <span className="numeric">{cursor ? formatRangeFromSite(cursor) : '—'}</span>
      </div>
    </div>
  )
}

function formatBearing({ eastM, northM }: EnuPoint2D): string {
  const bearing = (Math.atan2(eastM, northM) * 180) / Math.PI
  return `${((bearing + 360) % 360).toFixed(0).padStart(3, '0')}°`
}

function formatRangeFromSite({ eastM, northM }: EnuPoint2D): string {
  const metres = Math.hypot(eastM, northM)
  return metres >= 1_000 ? `${(metres / 1_000).toFixed(2)} km` : `${metres.toFixed(0)} m`
}
