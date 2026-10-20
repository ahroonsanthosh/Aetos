/**
 * Slippy-map tile maths, for drawing a real map under the editor canvas.
 *
 * The editor without a basemap is the reason the first version was unusable: you typed
 * coordinates you had no way to check, imported whatever was there, and only found out it
 * was the wrong place once a neighbourhood of houses appeared in the command view. A map
 * underneath makes the whole flow visual — you see where you are before you import, and you
 * see what you are tracing on top of.
 *
 * Pure functions only. The fetching and drawing live in `ui/editor/basemap.ts`, so the
 * projection maths here can be tested without a network or a canvas.
 */

import type { Ground } from '@/types';
import { latLonToEnu, type LatLon } from './geo';

/** Standard Web Mercator tile size, px. */
export const TILE_SIZE = 256;

/** Ground resolution at zoom 0, equator: metres per pixel. */
const EQUATOR_METRES_PER_PIXEL = 156_543.033_928_04;

export interface TileRef {
  x: number;
  y: number;
  z: number;
}

export function lonToTileX(longitude: number, zoom: number): number {
  return ((longitude + 180) / 360) * 2 ** zoom;
}

export function latToTileY(latitude: number, zoom: number): number {
  const rad = (latitude * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * 2 ** zoom;
}

export function tileXToLon(x: number, zoom: number): number {
  return (x / 2 ** zoom) * 360 - 180;
}

export function tileYToLat(y: number, zoom: number): number {
  const n = Math.PI - (2 * Math.PI * y) / 2 ** zoom;
  return (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
}

/**
 * The zoom level whose tile pixels are closest to the view's own pixel scale.
 *
 * Picking this from the view rather than fixing it means zooming the editor fetches sharper
 * tiles, the way any map does — and, more practically, that a 200 m compound and a 4 km
 * refinery both get a legible basemap instead of one being a blur and the other a mosaic.
 */
export function zoomForScale(metresPerPixel: number, latitude: number, maxZoom = 19): number {
  if (metresPerPixel <= 0) return maxZoom;
  const atLatitude = (EQUATOR_METRES_PER_PIXEL * Math.cos((latitude * Math.PI) / 180)) / metresPerPixel;
  const zoom = Math.log2(atLatitude);
  return Math.max(0, Math.min(maxZoom, Math.round(zoom)));
}

/**
 * Every tile needed to cover a lat/lon bounding box at one zoom.
 *
 * Capped: a request that would need more than `maxTiles` is a sign the zoom is too high for
 * the area on screen, and fetching hundreds of tiles would be both slow and a breach of the
 * tile server's usage policy. The caller drops a zoom level and asks again.
 */
export function tilesForBounds(
  bounds: { south: number; west: number; north: number; east: number },
  zoom: number,
  maxTiles = 64,
): TileRef[] {
  const minX = Math.floor(lonToTileX(bounds.west, zoom));
  const maxX = Math.floor(lonToTileX(bounds.east, zoom));
  // Tile y increases southward, so the north edge gives the smaller index.
  const minY = Math.floor(latToTileY(bounds.north, zoom));
  const maxY = Math.floor(latToTileY(bounds.south, zoom));

  const span = 2 ** zoom;
  const tiles: TileRef[] = [];

  for (let x = minX; x <= maxX; x++) {
    for (let y = minY; y <= maxY; y++) {
      if (y < 0 || y >= span) continue; // No tiles beyond the poles.
      // Longitude wraps; x is taken modulo the world width so a view crossing the
      // antimeridian still resolves to real tiles.
      const wrappedX = ((x % span) + span) % span;
      tiles.push({ x: wrappedX, y, z: zoom });
      if (tiles.length > maxTiles) return tiles.slice(0, maxTiles);
    }
  }

  return tiles;
}

/**
 * A tile's ground footprint in the site's local ENU frame.
 *
 * Converted corner by corner through `latLonToEnu` rather than assuming the tile grid is
 * linear in ENU. Over a site-sized area the difference is sub-pixel, but doing it properly
 * costs nothing and means the basemap stays aligned with traced geometry as the view moves
 * away from the datum.
 */
export function tileBoundsEnu(tile: TileRef, datum: LatLon): { min: Ground; max: Ground } {
  const west = tileXToLon(tile.x, tile.z);
  const east = tileXToLon(tile.x + 1, tile.z);
  const north = tileYToLat(tile.y, tile.z);
  const south = tileYToLat(tile.y + 1, tile.z);

  const topLeft = latLonToEnu({ latitude: north, longitude: west }, datum);
  const bottomRight = latLonToEnu({ latitude: south, longitude: east }, datum);

  return {
    min: { e: topLeft.e, n: bottomRight.n },
    max: { e: bottomRight.e, n: topLeft.n },
  };
}

/**
 * OpenStreetMap's standard raster tile URL.
 *
 * Their tile usage policy requires an identifying User-Agent or Referer and forbids heavy
 * or bulk use. A browser sends a Referer automatically, and the editor only fetches tiles
 * for what is actually on screen, which is the light, interactive use the policy permits.
 * Anything approaching production traffic should move to a paid tile host or self-hosted
 * tiles — the existing Aetos frontend already vendors Protomaps basemaps for exactly that
 * reason (`install-basemap.sh`).
 */
export function osmTileUrl(tile: TileRef): string {
  return `https://tile.openstreetmap.org/${tile.z}/${tile.x}/${tile.y}.png`;
}
