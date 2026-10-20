/**
 * Tests the pure half of OSM import: turning an Overpass response into draft assets. No
 * network involved — the fixture below is the shape a real `out geom` query returns, close
 * enough to catch a parsing regression without depending on Overpass being reachable.
 */

import { describe, expect, it } from 'vitest';
import { buildOverpassQuery, overpassToAssets } from './osm';

const DATUM = { latitude: 51.8985, longitude: -8.4756 };

describe('buildOverpassQuery', () => {
  it('embeds the bounding box and asks for geometry', () => {
    const query = buildOverpassQuery({ south: 1, west: 2, north: 3, east: 4 });
    expect(query).toContain('1,2,3,4');
    expect(query).toContain('building');
    expect(query).toContain('out geom');
  });
});

describe('overpassToAssets', () => {
  it('converts a tagged building way into an asset with a real footprint', () => {
    const response = {
      elements: [
        {
          type: 'way' as const,
          id: 111,
          tags: { building: 'yes', name: 'Test Building', height: '12' },
          geometry: [
            { lat: DATUM.latitude, lon: DATUM.longitude },
            { lat: DATUM.latitude, lon: DATUM.longitude + 0.0003 },
            { lat: DATUM.latitude + 0.0002, lon: DATUM.longitude + 0.0003 },
            { lat: DATUM.latitude + 0.0002, lon: DATUM.longitude },
            // closed ring — repeats the first node
            { lat: DATUM.latitude, lon: DATUM.longitude },
          ],
        },
      ],
    };

    const result = overpassToAssets(response, DATUM);

    expect(result.assets).toHaveLength(1);
    expect(result.skipped).toBe(0);

    const asset = result.assets[0]!;
    expect(asset.name).toBe('Test Building');
    expect(asset.footprint.height).toBe(12);
    expect(asset.footprintPolygon).toHaveLength(4); // duplicate closing node dropped
    expect(asset.footprint.width).toBeGreaterThan(0);
    expect(asset.footprint.depth).toBeGreaterThan(0);
  });

  it('maps power=substation to the substation type regardless of the building tag', () => {
    const response = {
      elements: [
        {
          type: 'way' as const,
          id: 222,
          tags: { building: 'yes', power: 'substation' },
          geometry: [
            { lat: DATUM.latitude, lon: DATUM.longitude },
            { lat: DATUM.latitude, lon: DATUM.longitude + 0.0002 },
            { lat: DATUM.latitude + 0.0002, lon: DATUM.longitude + 0.0002 },
          ],
        },
      ],
    };

    const result = overpassToAssets(response, DATUM);
    expect(result.assets[0]!.type).toBe('substation');
  });

  it('falls back to a default height when no height or levels tag is present', () => {
    const response = {
      elements: [
        {
          type: 'way' as const,
          id: 333,
          tags: { building: 'yes' },
          geometry: [
            { lat: DATUM.latitude, lon: DATUM.longitude },
            { lat: DATUM.latitude, lon: DATUM.longitude + 0.0002 },
            { lat: DATUM.latitude + 0.0002, lon: DATUM.longitude + 0.0002 },
          ],
        },
      ],
    };

    const result = overpassToAssets(response, DATUM);
    expect(result.assets[0]!.footprint.height).toBeGreaterThan(0);
  });

  it('derives height from building:levels when height is absent', () => {
    const response = {
      elements: [
        {
          type: 'way' as const,
          id: 444,
          tags: { building: 'yes', 'building:levels': '4' },
          geometry: [
            { lat: DATUM.latitude, lon: DATUM.longitude },
            { lat: DATUM.latitude, lon: DATUM.longitude + 0.0002 },
            { lat: DATUM.latitude + 0.0002, lon: DATUM.longitude + 0.0002 },
          ],
        },
      ],
    };

    const result = overpassToAssets(response, DATUM);
    expect(result.assets[0]!.footprint.height).toBe(12); // 4 levels * 3 m
  });

  it('reads a relation from its outer member ring', () => {
    const response = {
      elements: [
        {
          type: 'relation' as const,
          id: 555,
          tags: { building: 'yes' },
          members: [
            {
              type: 'way',
              role: 'outer',
              geometry: [
                { lat: DATUM.latitude, lon: DATUM.longitude },
                { lat: DATUM.latitude, lon: DATUM.longitude + 0.0002 },
                { lat: DATUM.latitude + 0.0002, lon: DATUM.longitude + 0.0002 },
              ],
            },
          ],
        },
      ],
    };

    const result = overpassToAssets(response, DATUM);
    expect(result.assets).toHaveLength(1);
  });

  it('counts an element with no usable ring as skipped, not silently dropped', () => {
    const response = {
      elements: [
        { type: 'way' as const, id: 666, tags: { building: 'yes' }, geometry: [{ lat: 0, lon: 0 }] },
      ],
    };

    const result = overpassToAssets(response, DATUM);
    expect(result.assets).toHaveLength(0);
    expect(result.skipped).toBe(1);
  });

  it('suggests a boundary enclosing every imported building', () => {
    const response = {
      elements: [
        {
          type: 'way' as const,
          id: 1,
          tags: { building: 'yes' },
          geometry: [
            { lat: DATUM.latitude, lon: DATUM.longitude },
            { lat: DATUM.latitude, lon: DATUM.longitude + 0.0002 },
            { lat: DATUM.latitude + 0.0002, lon: DATUM.longitude + 0.0002 },
          ],
        },
        {
          type: 'way' as const,
          id: 2,
          tags: { building: 'yes' },
          geometry: [
            { lat: DATUM.latitude + 0.002, lon: DATUM.longitude + 0.002 },
            { lat: DATUM.latitude + 0.002, lon: DATUM.longitude + 0.0022 },
            { lat: DATUM.latitude + 0.0022, lon: DATUM.longitude + 0.0022 },
          ],
        },
      ],
    };

    const result = overpassToAssets(response, DATUM);
    expect(result.suggestedBoundary.length).toBeGreaterThanOrEqual(3);
  });
});
