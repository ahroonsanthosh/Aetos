/**
 * Coverage, protection and readiness tests.
 *
 * The claim being pinned here is that killing a sensor is a single consequence with three
 * visible effects — the coverage map, the affected assets' protection scores, and the
 * readiness figure all move together, because all three are derived from the same model.
 * If they ever stop moving together, the interface is asserting three independent opinions
 * about one situation, which is precisely the failure the design exists to avoid.
 */

import { describe, expect, it } from 'vitest';

import { DEMO_SITE } from '@/site/demo-site';
import type { Sensor } from '@/types';
import {
  aggregateCoverageAt,
  assetProtectionScore,
  buildCoverageGrid,
  buildSystemHealth,
  qualityFor,
  sensorCoverageAt,
} from './coverage';
import { WEATHER } from './weather';

const SENSORS = DEMO_SITE.sensors;
const SUBSTATION = DEMO_SITE.assets.find((a) => a.id === 'SUB-01')!;

function withStatus(id: string, status: Sensor['status']): Sensor[] {
  return SENSORS.map((s) =>
    s.id === id ? { ...s, status, effectiveness: status === 'offline' ? 0 : s.effectiveness } : s,
  );
}

describe('sensorCoverageAt', () => {
  const radar = SENSORS.find((s) => s.id === 'RADAR-01')!;

  it('returns nothing beyond maximum range', () => {
    const beyond = { e: radar.position.e, n: radar.position.n + radar.coverage.maxRange + 200, u: 60 };
    expect(sensorCoverageAt(radar, beyond)).toBe(0);
  });

  it('returns nothing inside the minimum range', () => {
    const inside = { e: radar.position.e + 5, n: radar.position.n, u: 20 };
    expect(sensorCoverageAt(radar, inside)).toBe(0);
  });

  it('returns nothing for an offline sensor, wherever the target is', () => {
    const offline: Sensor = { ...radar, status: 'offline', effectiveness: 0 };
    const target = { e: radar.position.e + 300, n: radar.position.n, u: 60 };

    expect(sensorCoverageAt(radar, target)).toBeGreaterThan(0);
    expect(sensorCoverageAt(offline, target)).toBe(0);
  });

  it('respects the azimuth sector of a directional sensor', () => {
    const camera = SENSORS.find((s) => s.id === 'CAM-01')!;
    // The camera looks west; a target the same distance east is outside its sector.
    const west = { e: camera.position.e - 300, n: camera.position.n, u: 40 };
    const east = { e: camera.position.e + 300, n: camera.position.n, u: 40 };

    expect(sensorCoverageAt(camera, west)).toBeGreaterThan(0);
    expect(sensorCoverageAt(camera, east)).toBe(0);
  });
});

describe('aggregateCoverageAt', () => {
  it('never exceeds certainty however many sensors overlap', () => {
    const point = { e: 0, n: 0, u: 60 };
    const many = [...SENSORS, ...SENSORS, ...SENSORS];
    expect(aggregateCoverageAt(many, point)).toBeLessThanOrEqual(1);
  });

  it('is better with two sensors than with either alone', () => {
    const point = { e: 100, n: 100, u: 60 };
    const a = SENSORS.filter((s) => s.id === 'RADAR-01');
    const b = SENSORS.filter((s) => s.id === 'RADAR-02');

    const both = aggregateCoverageAt([...a, ...b], point);
    expect(both).toBeGreaterThanOrEqual(aggregateCoverageAt(a, point));
    expect(both).toBeGreaterThanOrEqual(aggregateCoverageAt(b, point));
  });
});

describe('losing a sensor', () => {
  const healthy = SENSORS;
  const degraded = withStatus('RADAR-02', 'offline');

  it('opens a hole in the coverage map', () => {
    const before = buildCoverageGrid(healthy, 700, 100);
    const after = buildCoverageGrid(degraded, 700, 100);

    const gapsBefore = before.cells.filter((c) => c.quality === 'gap').length;
    const gapsAfter = after.cells.filter((c) => c.quality === 'gap').length;

    expect(gapsAfter).toBeGreaterThanOrEqual(gapsBefore);

    const meanBefore = before.cells.reduce((s, c) => s + c.value, 0) / before.cells.length;
    const meanAfter = after.cells.reduce((s, c) => s + c.value, 0) / after.cells.length;
    expect(meanAfter).toBeLessThan(meanBefore);
  });

  it('lowers asset protection at the same time', () => {
    const before = assetProtectionScore(SUBSTATION, healthy, DEMO_SITE.resources);
    const after = assetProtectionScore(SUBSTATION, degraded, DEMO_SITE.resources);
    expect(after).toBeLessThanOrEqual(before);
  });

  it('lowers readiness and names the failure in plain language', () => {
    const gridBefore = buildCoverageGrid(healthy, 700, 100);
    const gridAfter = buildCoverageGrid(degraded, 700, 100);

    const before = buildSystemHealth(
      healthy, gridBefore, DEMO_SITE.resources, WEATHER.clear, DEMO_SITE.assets,
    );
    const after = buildSystemHealth(
      degraded, gridAfter, DEMO_SITE.resources, WEATHER.clear, DEMO_SITE.assets,
    );

    expect(after.readiness).toBeLessThan(before.readiness);
    // A number on its own is not a diagnosis. The operator gets a sentence and an action.
    expect(after.primaryWeakness).toContain('Radar 02');
    expect(after.recommendedAction).toBeTruthy();
  });
});

describe('weather', () => {
  it('costs the cameras and leaves the radar alone in fog', () => {
    const fog = WEATHER.fog;
    expect(fog.sensorImpact.camera).toBeLessThan(0.3);
    expect(fog.sensorImpact.radar).toBe(1);
  });
});

describe('qualityFor', () => {
  it('bands coverage values', () => {
    expect(qualityFor(0.9)).toBe('strong');
    expect(qualityFor(0.4)).toBe('reduced');
    expect(qualityFor(0.05)).toBe('gap');
  });
});
