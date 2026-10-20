import { describe, expect, it } from 'vitest';
import type { Asset } from '@/types';
import { pointInPolygon } from '@/lib/geometry';
import { generateDefaultResources, generateDefaultSensors, generateDefaultZones } from './generate';

const SQUARE_BOUNDARY = [
  { e: -100, n: -100 },
  { e: 100, n: -100 },
  { e: 100, n: 100 },
  { e: -100, n: 100 },
];

function asset(overrides: Partial<Asset>): Asset {
  return {
    id: 'A', name: 'Asset', type: 'building', position: { e: 0, n: 0, u: 0 },
    footprint: { width: 10, depth: 10, height: 5 }, heading: 0, criticality: 5,
    status: 'operational', sensorsCovering: [], activeThreats: [], protectionScore: 0.5,
    ...overrides,
  };
}

describe('generateDefaultSensors', () => {
  it('returns nothing for a boundary too small to be a polygon', () => {
    expect(generateDefaultSensors([{ e: 0, n: 0 }])).toEqual([]);
  });

  it('places the requested number of sensors, all inside or on the boundary', () => {
    const sensors = generateDefaultSensors(SQUARE_BOUNDARY, 4);
    expect(sensors).toHaveLength(4);
    for (const sensor of sensors) {
      const onOrInside =
        pointInPolygon(sensor.position, SQUARE_BOUNDARY) ||
        Math.abs(Math.abs(sensor.position.e) - 100) < 1e-6 ||
        Math.abs(Math.abs(sensor.position.n) - 100) < 1e-6;
      expect(onOrInside).toBe(true);
    }
  });

  it('aims every sensor generally back toward the centroid', () => {
    const sensors = generateDefaultSensors(SQUARE_BOUNDARY, 4);
    for (const sensor of sensors) {
      const toCentre = Math.atan2(0 - sensor.position.e, 0 - sensor.position.n);
      const normalised = toCentre < 0 ? toCentre + Math.PI * 2 : toCentre;
      expect(sensor.coverage.bearing).toBeCloseTo(normalised, 3);
    }
  });

  it('gives every sensor a unique id', () => {
    const sensors = generateDefaultSensors(SQUARE_BOUNDARY, 6);
    expect(new Set(sensors.map((s) => s.id)).size).toBe(6);
  });

  it('never produces a sensor whose range is zero or negative', () => {
    const sensors = generateDefaultSensors(SQUARE_BOUNDARY, 4);
    for (const sensor of sensors) expect(sensor.coverage.maxRange).toBeGreaterThan(0);
  });
});

describe('generateDefaultResources', () => {
  it('returns two resources for a valid boundary, none for an empty one', () => {
    expect(generateDefaultResources(SQUARE_BOUNDARY)).toHaveLength(2);
    expect(generateDefaultResources([])).toHaveLength(0);
  });
});

describe('generateDefaultZones', () => {
  it('always produces a perimeter zone matching the boundary', () => {
    const { zones } = generateDefaultZones({ boundary: SQUARE_BOUNDARY, assets: [] });
    const perimeter = zones.find((z) => z.zoneClass === 'perimeter');
    expect(perimeter).toBeDefined();
    expect(perimeter!.polygon).toEqual(SQUARE_BOUNDARY);
  });

  it('gives a critical zone to a criticality-9 asset and none to a criticality-3 one', () => {
    const assets = [
      asset({ id: 'HIGH', criticality: 9, position: { e: 20, n: 20, u: 0 } }),
      asset({ id: 'LOW', criticality: 3, position: { e: -20, n: -20, u: 0 } }),
    ];
    const { zones, assignments } = generateDefaultZones({ boundary: SQUARE_BOUNDARY, assets });

    expect(assignments.HIGH).toBeDefined();
    expect(assignments.LOW).toBeUndefined();

    const criticalZone = zones.find((z) => z.id === assignments.HIGH);
    expect(criticalZone?.zoneClass).toBe('critical');
    expect(criticalZone?.protectsAssetId).toBe('HIGH');
  });

  it('gives a restricted (not critical) zone to a criticality-6 asset', () => {
    const assets = [asset({ id: 'MID', criticality: 6, position: { e: 0, n: 0, u: 0 } })];
    const { zones, assignments } = generateDefaultZones({ boundary: SQUARE_BOUNDARY, assets });
    const zone = zones.find((z) => z.id === assignments.MID);
    expect(zone?.zoneClass).toBe('restricted');
  });

  it('every generated zone polygon contains the asset it protects', () => {
    const assets = [asset({ id: 'A1', criticality: 10, position: { e: 40, n: -30, u: 0 } })];
    const { zones, assignments } = generateDefaultZones({ boundary: SQUARE_BOUNDARY, assets });
    const zone = zones.find((z) => z.id === assignments.A1)!;
    expect(pointInPolygon({ e: 40, n: -30 }, zone.polygon)).toBe(true);
  });

  it('produces no zone at all for an empty boundary and no qualifying assets', () => {
    const { zones } = generateDefaultZones({ boundary: [], assets: [] });
    expect(zones).toHaveLength(0);
  });
});
