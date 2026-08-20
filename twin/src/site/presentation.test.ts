import { describe, expect, it } from 'vitest';
import type { Asset, Sensor, Site } from '@/types';
import { computeSceneExtent, computeSiteExtent, deriveScenePresentation } from './presentation';

function baseSite(overrides: Partial<Site> = {}): Site {
  return {
    id: 'S', name: 'S', kind: 'Site', datum: { latitude: 0, longitude: 0 },
    boundary: [{ e: -50, n: -50 }, { e: 50, n: -50 }, { e: 50, n: 50 }, { e: -50, n: 50 }],
    assets: [], sensors: [], zones: [], resources: [], roads: [], arrays: [],
    ...overrides,
  };
}

function asset(overrides: Partial<Asset>): Asset {
  return {
    id: 'A', name: 'A', type: 'building', position: { e: 0, n: 0, u: 0 },
    footprint: { width: 10, depth: 10, height: 5 }, heading: 0, criticality: 5,
    status: 'operational', sensorsCovering: [], activeThreats: [], protectionScore: 0.5,
    ...overrides,
  };
}

function sensor(overrides: Partial<Sensor>): Sensor {
  return {
    id: 'SN', name: 'SN', type: 'radar', position: { e: 0, n: 0, u: 0 }, mastHeight: 10,
    status: 'online', health: 1, effectiveness: 1,
    coverage: { minRange: 0, maxRange: 100, bearing: 0, azimuthWidth: Math.PI * 2, minElevation: 0, maxElevation: 1 },
    ...overrides,
  };
}

describe('computeSiteExtent', () => {
  it('never returns less than the floor, for a tiny or empty site', () => {
    expect(computeSiteExtent(baseSite({ boundary: [] }))).toBeGreaterThanOrEqual(60);
  });

  it('grows to cover a boundary vertex further than the default floor', () => {
    const extent = computeSiteExtent(
      baseSite({ boundary: [{ e: -2000, n: 0 }, { e: 2000, n: 0 }, { e: 0, n: 2000 }] }),
    );
    expect(extent).toBeGreaterThanOrEqual(2000);
  });

  it('grows to cover an asset outside the boundary', () => {
    const extent = computeSiteExtent(
      baseSite({ assets: [asset({ position: { e: 900, n: 0, u: 0 }, footprint: { width: 4, depth: 4, height: 4 } })] }),
    );
    expect(extent).toBeGreaterThanOrEqual(900);
  });

  it('ignores sensor coverage — that is what the camera frames against', () => {
    // A 100 m compound with a 3 km radar is a 100 m site, not a 3 km one. Framing it as
    // the latter leaves the facility a smudge in the middle of an empty screen.
    const withRadar = baseSite({
      sensors: [
        sensor({
          position: { e: 0, n: 0, u: 0 },
          coverage: { minRange: 0, maxRange: 3000, bearing: 0, azimuthWidth: Math.PI * 2, minElevation: 0, maxElevation: 1 },
        }),
      ],
    });
    expect(computeSiteExtent(withRadar)).toBe(computeSiteExtent(baseSite()));
  });
});

describe('computeSceneExtent', () => {
  it('does grow to cover a sensor envelope reaching past the boundary', () => {
    const extent = computeSceneExtent(
      baseSite({
        sensors: [
          sensor({
            position: { e: 0, n: 0, u: 0 },
            coverage: { minRange: 0, maxRange: 3000, bearing: 0, azimuthWidth: Math.PI * 2, minElevation: 0, maxElevation: 1 },
          }),
        ],
      }),
    );
    expect(extent).toBeGreaterThanOrEqual(3000);
  });

  it('is never smaller than the physical site extent', () => {
    const site = baseSite({
      boundary: [{ e: -900, n: -900 }, { e: 900, n: -900 }, { e: 900, n: 900 }, { e: -900, n: 900 }],
    });
    expect(computeSceneExtent(site)).toBeGreaterThanOrEqual(computeSiteExtent(site));
  });
});

describe('deriveScenePresentation', () => {
  it('produces larger distances for a larger site', () => {
    const small = deriveScenePresentation(baseSite());
    const large = deriveScenePresentation(
      baseSite({ boundary: [{ e: -3000, n: -3000 }, { e: 3000, n: -3000 }, { e: 3000, n: 3000 }, { e: -3000, n: 3000 }] }),
    );

    expect(large.siteExtent).toBeGreaterThan(small.siteExtent);
    expect(large.cameraPoses.overview.distance).toBeGreaterThan(small.cameraPoses.overview.distance);
    expect(large.groundExtent).toBeGreaterThan(small.groundExtent);
    expect(large.fogFar).toBeGreaterThan(small.fogFar);
  });

  it('always frames the whole ground plane beyond the site extent', () => {
    const p = deriveScenePresentation(
      baseSite({ boundary: [{ e: -1800, n: -1800 }, { e: 1800, n: -1800 }, { e: 1800, n: 1800 }, { e: -1800, n: 1800 }] }),
    );
    expect(p.groundExtent).toBeGreaterThan(p.siteExtent);
    expect(p.cameraMaxDistance).toBeGreaterThan(p.siteExtent);
    expect(p.coverageExtent).toBeGreaterThan(p.siteExtent * 0.9);
  });

  it('keeps a long-range sensor inside the drawn world without loosening the framing', () => {
    const compound = baseSite({
      boundary: [{ e: -100, n: -100 }, { e: 100, n: -100 }, { e: 100, n: 100 }, { e: -100, n: 100 }],
    });
    const withRadar = {
      ...compound,
      sensors: [
        sensor({
          position: { e: 0, n: 0, u: 0 },
          coverage: { minRange: 0, maxRange: 3000, bearing: 0, azimuthWidth: Math.PI * 2, minElevation: 0, maxElevation: 1 },
        }),
      ],
    };

    const plain = deriveScenePresentation(compound);
    const radar = deriveScenePresentation(withRadar);

    // Framing is unchanged — the compound is still the subject.
    expect(radar.cameraPoses.overview.distance).toBeCloseTo(plain.cameraPoses.overview.distance, 6);
    // But the world grew to hold the envelope, so the dome never hangs over the void.
    expect(radar.groundExtent).toBeGreaterThan(3000);
    expect(radar.cameraMaxDistance).toBeGreaterThan(3000);
  });

  it('snaps grid spacing to round numbers rather than an arbitrary fraction', () => {
    const p = deriveScenePresentation(baseSite());
    const ladder = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000];
    expect(ladder).toContain(p.gridMinor);
    expect(ladder).toContain(p.gridMajor);
    expect(p.gridMajor).toBeGreaterThan(p.gridMinor);
  });

  it('keeps every mode ordered overview > tactical/system spread appropriately at any scale', () => {
    for (const boundary of [
      [{ e: -100, n: -100 }, { e: 100, n: -100 }, { e: 100, n: 100 }, { e: -100, n: 100 }],
      [{ e: -4000, n: -4000 }, { e: 4000, n: -4000 }, { e: 4000, n: 4000 }, { e: -4000, n: 4000 }],
    ]) {
      const p = deriveScenePresentation(baseSite({ boundary }));
      expect(p.cameraPoses.overview.distance).toBeGreaterThan(p.cameraPoses.tactical.distance);
      expect(p.cameraPoses.system.distance).toBeGreaterThan(p.cameraPoses.overview.distance);
      expect(p.cameraPoses.tactical.distance).toBeGreaterThan(p.cameraPoses.threat.distance);
    }
  });
});
