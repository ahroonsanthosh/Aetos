/**
 * Converting the PPI's viewport into a basemap camera.
 *
 * The display thinks in metres from the site; a tiled basemap thinks in Web Mercator zoom
 * levels at a latitude. This is the one place the two meet, and it is a pure function so the
 * conversion can be checked without a browser, a GPU or a tile server.
 *
 * The conversion matters more than it looks. Web Mercator's scale factor is `1 / cos(latitude)`
 * — at 45° that is 1.41× — so a basemap laid under an ENU-metre display *without* accounting
 * for it is wrong by nearly half, and wrong in a way that looks plausible: the tiles render,
 * the roads look like roads, and every track sits somewhere it is not. That failure is silent
 * and the operator has no way to detect it from the display.
 *
 * The PPI stays authoritative. The basemap follows it, never the reverse, so turning the
 * basemap off — or failing to load it at all — leaves the display exactly as it was.
 */

import type { LocalFrame } from '../../data/models/geo'
import type { MapViewport } from './useMapViewport'
import { VIEW_MARGIN } from './useMapViewport'

/**
 * Ground resolution in metres per pixel at zoom 0 on the equator, for a 512 px tile.
 *
 * MapLibre's tiles are 512 px, so this is the equatorial circumference divided by 512 rather
 * than by the 256 that the same constant is usually quoted with. Halving it by mistake puts
 * the basemap out by exactly one zoom level — visible immediately, unlike the latitude error
 * above, which is the only reason that mistake is the safer one.
 */
const METRES_PER_PIXEL_AT_ZOOM_0 = 78271.516964020697

export interface BasemapCamera {
  readonly longitude: number
  readonly latitude: number
  readonly zoom: number
}

/**
 * The camera that puts a basemap in exact register with the PPI.
 *
 * `sidePx` is the rendered size of the square viewBox in CSS pixels — the shorter of the
 * container's two dimensions, since `preserveAspectRatio="xMidYMid meet"` fits the square
 * inside the container and centres it.
 */
export function cameraFor(
  viewport: MapViewport,
  frame: LocalFrame,
  sidePx: number,
): BasemapCamera {
  const centre = frame.toGeodetic(viewport.centre)

  // The SVG spans this many metres across `sidePx` pixels, margin included — the margin is
  // part of what is on screen, so leaving it out would zoom the basemap in by 8%.
  const spanM = viewport.rangeM * VIEW_MARGIN * 2
  const metresPerPixel = spanM / Math.max(1, sidePx)

  // metresPerPixel = METRES_PER_PIXEL_AT_ZOOM_0 · cos(latitude) / 2^zoom
  const latitudeRadians = (centre.latitude * Math.PI) / 180
  const scale = (METRES_PER_PIXEL_AT_ZOOM_0 * Math.cos(latitudeRadians)) / metresPerPixel

  return {
    longitude: centre.longitude,
    latitude: centre.latitude,
    // Guarded: at extreme zoom-out `scale` can fall below 1 and the log go negative, and a
    // negative zoom is rejected rather than clamped by some renderers.
    zoom: Math.max(0, Math.log2(Math.max(scale, Number.MIN_VALUE))),
  }
}
