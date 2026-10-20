'use client';

/**
 * Fetching and caching map tiles for the editor canvas.
 *
 * Kept out of `TraceCanvas` because tile loading is asynchronous and the canvas draw is
 * not: the canvas asks for whatever tiles are ready *right now* and draws those, and a
 * tile that arrives later triggers a redraw rather than blocking one. That is what stops a
 * slow tile server making the whole editor feel stuck.
 *
 * The cache is module-level and unbounded within a session, deliberately: a site editing
 * session covers one small area, so the working set is tens of tiles, and re-fetching a
 * tile the operator has already seen would be both slower and ruder to the tile server
 * than keeping it.
 */

import { osmTileUrl, type TileRef } from '@/site/import/tiles';

type TileState =
  | { status: 'loading' }
  | { status: 'ready'; image: HTMLImageElement }
  | { status: 'failed' };

const cache = new Map<string, TileState>();

function keyOf(tile: TileRef): string {
  return `${tile.z}/${tile.x}/${tile.y}`;
}

/**
 * The image for a tile if it is already loaded; otherwise starts loading it and returns
 * null. `onLoad` fires when a newly requested tile arrives, so the caller can redraw.
 */
export function getTile(tile: TileRef, onLoad: () => void): HTMLImageElement | null {
  const key = keyOf(tile);
  const existing = cache.get(key);

  if (existing?.status === 'ready') return existing.image;
  if (existing) return null; // loading or permanently failed

  cache.set(key, { status: 'loading' });

  const image = new Image();
  // Tiles are drawn into a canvas that is never read back via getImageData, so this is not
  // strictly required — but requesting CORS keeps the canvas untainted, which leaves the
  // door open for an export-to-image feature later without a silent security failure.
  image.crossOrigin = 'anonymous';

  image.onload = () => {
    cache.set(key, { status: 'ready', image });
    onLoad();
  };

  image.onerror = () => {
    // Marked failed rather than deleted: retrying a tile that 404s or is blocked would
    // loop forever, and a missing tile is a cosmetic gap, not an error worth surfacing.
    cache.set(key, { status: 'failed' });
  };

  image.src = osmTileUrl(tile);
  return null;
}

/** How many of the given tiles are loaded, for a "map still loading" indicator. */
export function readyCount(tiles: TileRef[]): number {
  let ready = 0;
  for (const tile of tiles) {
    if (cache.get(keyOf(tile))?.status === 'ready') ready += 1;
  }
  return ready;
}

/** Test seam / memory escape hatch. */
export function clearTileCache(): void {
  cache.clear();
}
