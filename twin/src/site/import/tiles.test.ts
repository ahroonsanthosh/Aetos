import { describe, expect, it } from 'vitest';
import {
  latToTileY,
  lonToTileX,
  osmTileUrl,
  tileBoundsEnu,
  tileXToLon,
  tileYToLat,
  tilesForBounds,
  zoomForScale,
} from './tiles';
import { boundingBoxAround } from './geo';

const CORK = { latitude: 51.8985, longitude: -8.4756 };

describe('tile coordinate conversion', () => {
  it('places the antimeridian and prime meridian correctly at zoom 1', () => {
    expect(lonToTileX(-180, 1)).toBeCloseTo(0, 9);
    expect(lonToTileX(0, 1)).toBeCloseTo(1, 9);
    expect(lonToTileX(180, 1)).toBeCloseTo(2, 9);
  });

  it('puts the equator at the vertical middle of the world', () => {
    expect(latToTileY(0, 1)).toBeCloseTo(1, 9);
  });

  it('round-trips longitude through tileXToLon', () => {
    for (const lon of [-179, -90, -8.4756, 0, 45.5, 179]) {
      for (const z of [2, 8, 14]) {
        expect(tileXToLon(lonToTileX(lon, z), z)).toBeCloseTo(lon, 8);
      }
    }
  });

  it('round-trips latitude through tileYToLat', () => {
    for (const lat of [-70, -33.9, 0, 51.8985, 70]) {
      for (const z of [2, 8, 14]) {
        expect(tileYToLat(latToTileY(lat, z), z)).toBeCloseTo(lat, 6);
      }
    }
  });

  it('increases tile y as latitude decreases', () => {
    expect(latToTileY(60, 10)).toBeLessThan(latToTileY(50, 10));
  });
});

describe('zoomForScale', () => {
  it('returns a higher zoom for a finer view scale', () => {
    const coarse = zoomForScale(50, CORK.latitude);
    const fine = zoomForScale(0.5, CORK.latitude);
    expect(fine).toBeGreaterThan(coarse);
  });

  it('stays within the valid zoom range for absurd inputs', () => {
    expect(zoomForScale(1e9, CORK.latitude)).toBeGreaterThanOrEqual(0);
    expect(zoomForScale(1e-9, CORK.latitude)).toBeLessThanOrEqual(19);
    expect(zoomForScale(0, CORK.latitude)).toBeLessThanOrEqual(19);
    expect(zoomForScale(-5, CORK.latitude)).toBeLessThanOrEqual(19);
  });

  it('picks a zoom whose ground resolution is close to the requested one', () => {
    // At zoom z, metres/px = 156543.03 * cos(lat) / 2^z. Ask for ~1 m/px and check the
    // chosen zoom lands within a factor of two either way.
    const z = zoomForScale(1, CORK.latitude);
    const actual = (156_543.033_928_04 * Math.cos((CORK.latitude * Math.PI) / 180)) / 2 ** z;
    expect(actual).toBeGreaterThan(0.5);
    expect(actual).toBeLessThan(2);
  });
});

describe('tilesForBounds', () => {
  it('covers a small area with at least one tile', () => {
    const tiles = tilesForBounds(boundingBoxAround(CORK, 300), 16);
    expect(tiles.length).toBeGreaterThan(0);
    for (const t of tiles) expect(t.z).toBe(16);
  });

  it('needs more tiles for a larger area at the same zoom', () => {
    const small = tilesForBounds(boundingBoxAround(CORK, 200), 16);
    const large = tilesForBounds(boundingBoxAround(CORK, 2000), 16);
    expect(large.length).toBeGreaterThan(small.length);
  });

  it('caps the number of tiles rather than issuing an unbounded fetch', () => {
    const tiles = tilesForBounds(boundingBoxAround(CORK, 50_000), 18, 24);
    expect(tiles.length).toBeLessThanOrEqual(24);
  });

  it('never returns a tile index outside the world at that zoom', () => {
    const tiles = tilesForBounds({ south: -89, west: -180, north: 89, east: 180 }, 2);
    for (const t of tiles) {
      expect(t.x).toBeGreaterThanOrEqual(0);
      expect(t.x).toBeLessThan(2 ** 2);
      expect(t.y).toBeGreaterThanOrEqual(0);
      expect(t.y).toBeLessThan(2 ** 2);
    }
  });
});

describe('tileBoundsEnu', () => {
  it('gives a tile a positive extent with max north of min', () => {
    const tile = { x: Math.floor(lonToTileX(CORK.longitude, 16)), y: Math.floor(latToTileY(CORK.latitude, 16)), z: 16 };
    const bounds = tileBoundsEnu(tile, CORK);
    expect(bounds.max.e).toBeGreaterThan(bounds.min.e);
    expect(bounds.max.n).toBeGreaterThan(bounds.min.n);
  });

  it('places the tile containing the datum across the ENU origin', () => {
    const tile = { x: Math.floor(lonToTileX(CORK.longitude, 16)), y: Math.floor(latToTileY(CORK.latitude, 16)), z: 16 };
    const bounds = tileBoundsEnu(tile, CORK);
    expect(bounds.min.e).toBeLessThanOrEqual(0);
    expect(bounds.max.e).toBeGreaterThanOrEqual(0);
    expect(bounds.min.n).toBeLessThanOrEqual(0);
    expect(bounds.max.n).toBeGreaterThanOrEqual(0);
  });

  it('makes adjacent tiles share an edge, so the basemap has no seams', () => {
    const x = Math.floor(lonToTileX(CORK.longitude, 16));
    const y = Math.floor(latToTileY(CORK.latitude, 16));
    const left = tileBoundsEnu({ x, y, z: 16 }, CORK);
    const right = tileBoundsEnu({ x: x + 1, y, z: 16 }, CORK);
    const below = tileBoundsEnu({ x, y: y + 1, z: 16 }, CORK);

    expect(right.min.e).toBeCloseTo(left.max.e, 6);
    expect(below.max.n).toBeCloseTo(left.min.n, 6);
  });
});

describe('osmTileUrl', () => {
  it('builds the standard z/x/y path', () => {
    expect(osmTileUrl({ x: 3, y: 5, z: 7 })).toBe('https://tile.openstreetmap.org/7/3/5.png');
  });
});
