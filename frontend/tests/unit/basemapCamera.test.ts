/**
 * Basemap camera: the conversion that keeps tiles in register with the PPI.
 *
 * The failure this guards against is silent. A basemap laid under an ENU-metre display without
 * accounting for Web Mercator's `1 / cos(latitude)` scale factor still renders — the tiles
 * load, the roads look like roads — and every track sits somewhere it is not. An operator has
 * no way to detect that from the display, which is why the arithmetic is checked here rather
 * than by looking at a screenshot and deciding it seems about right.
 */

import { describe, expect, it } from 'vitest'

import { LocalFrame } from '../../src/data/models/geo'
import { cameraFor } from '../../src/components/map/basemapCamera'
import { VIEW_MARGIN } from '../../src/components/map/useMapViewport'

const SIDE_PX = 900

function frameAt(latitude: number): LocalFrame {
  return new LocalFrame({ latitude, longitude: 0, altitude_m: 0 })
}

function viewport(rangeM: number, centre = { eastM: 0, northM: 0 }) {
  return { rangeM, centre, isDefault: false }
}

/** Ground resolution MapLibre actually renders at, for a 512 px tile scheme. */
function metresPerPixel(zoom: number, latitude: number): number {
  return (78271.516964020697 * Math.cos((latitude * Math.PI) / 180)) / 2 ** zoom
}

describe('scale', () => {
  it('produces a zoom whose ground resolution matches the display exactly', () => {
    const latitude = 45
    const camera = cameraFor(viewport(5_000), frameAt(latitude), SIDE_PX)

    const expected = (5_000 * VIEW_MARGIN * 2) / SIDE_PX
    expect(metresPerPixel(camera.zoom, camera.latitude)).toBeCloseTo(expected, 6)
  })

  it('accounts for latitude rather than assuming the equator', () => {
    // The whole point. At 60 degrees a Mercator pixel covers half the ground it does at the
    // equator, so the same span of metres needs a zoom one level different. A conversion that
    // ignored latitude would return the same zoom for both.
    const equator = cameraFor(viewport(5_000), frameAt(0), SIDE_PX)
    const high = cameraFor(viewport(5_000), frameAt(60), SIDE_PX)

    expect(high.zoom - equator.zoom).toBeCloseTo(-1, 6)
  })

  it('includes the viewBox margin, which is on screen too', () => {
    // Leaving the margin out would zoom the basemap in by 8% — small enough to look fine and
    // large enough to put a track 400 m from where it is at a 5 km range.
    const camera = cameraFor(viewport(5_000), frameAt(45), SIDE_PX)
    const withoutMargin = (5_000 * 2) / SIDE_PX

    expect(metresPerPixel(camera.zoom, camera.latitude)).not.toBeCloseTo(withoutMargin, 3)
  })

  it('zooms in by exactly one level when the range halves', () => {
    const wide = cameraFor(viewport(5_000), frameAt(45), SIDE_PX)
    const close = cameraFor(viewport(2_500), frameAt(45), SIDE_PX)

    expect(close.zoom - wide.zoom).toBeCloseTo(1, 6)
  })

  it('never returns a negative zoom', () => {
    const camera = cameraFor(viewport(50_000), frameAt(45), 10)

    expect(camera.zoom).toBeGreaterThanOrEqual(0)
  })
})

describe('centring', () => {
  it('centres on the site when the viewport is site-centred', () => {
    const camera = cameraFor(viewport(5_000), frameAt(45), SIDE_PX)

    expect(camera.latitude).toBeCloseTo(45, 9)
    expect(camera.longitude).toBeCloseTo(0, 9)
  })

  it('follows a panned viewport, and the round trip is exact', () => {
    const frame = frameAt(45)
    const centre = { eastM: 3_000, northM: -1_500 }
    const camera = cameraFor(viewport(2_000, centre), frame, SIDE_PX)

    const backToEnu = frame.toEnu({
      latitude: camera.latitude,
      longitude: camera.longitude,
      altitude_m: 0,
    })

    expect(backToEnu.eastM).toBeCloseTo(centre.eastM, 6)
    expect(backToEnu.northM).toBeCloseTo(centre.northM, 6)
  })

  it('moves north for a northward pan, not south', () => {
    // A sign error here mirrors the basemap about the site and is entirely plausible-looking
    // until an operator compares a track against a road.
    const camera = cameraFor(
      viewport(2_000, { eastM: 0, northM: 4_000 }),
      frameAt(45),
      SIDE_PX,
    )

    expect(camera.latitude).toBeGreaterThan(45)
  })
})
