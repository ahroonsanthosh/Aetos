import { describe, expect, it } from 'vitest';

import type { Snapshot } from '@/types';
import type { SensorView, SiteView, TrackView } from './api';
import { adviceFor, buildSite, classify, toTrack, UNBOUNDED_CEILING_M } from './convert';
import { LiveSource } from './LiveSource';

const REF = { latitude: 51.8971, longitude: -8.4697, altitude_m: 60 };

const SITE: SiteView = {
  site_id: 'test-site',
  name: 'Test range',
  reference: REF,
  protected_radius_m: 1500,
  zones: [
    {
      zone_id: 'core',
      name: 'Core',
      kind: 'protected',
      geometry: { shape: 'circle', centre: { latitude: REF.latitude, longitude: REF.longitude }, radius_m: 500 },
      floor_m: null,
      ceiling_m: null,
    },
    {
      zone_id: 'east',
      name: 'East strip',
      kind: 'restricted',
      geometry: {
        shape: 'polygon',
        // Clockwise on purpose: the converter must normalise it.
        vertices: [
          { latitude: 51.899, longitude: -8.455 },
          { latitude: 51.899, longitude: -8.445 },
          { latitude: 51.895, longitude: -8.445 },
          { latitude: 51.895, longitude: -8.455 },
        ],
      },
      floor_m: 60,
      ceiling_m: 260,
    },
  ],
};

const SENSORS: SensorView[] = [
  {
    sensor_id: 'radar_north',
    site_id: 'test-site',
    registry_key: 'sim_radar_medium',
    hardware_driver: null,
    sensor_type: 'simulated_radar',
    position: { latitude: 51.9079, longitude: -8.4697 },
    height_m: 12,
    boresight_deg: 0,
    enabled: true,
    coverage: { min_range_m: 150, max_range_m: 8000, azimuth_extent_deg: 360, elevation_min_deg: -5, elevation_max_deg: 40 },
  },
  {
    sensor_id: 'eo_ir_west',
    site_id: 'test-site',
    registry_key: 'sim_eo_ir_narrow',
    hardware_driver: null,
    sensor_type: 'simulated_eo_ir',
    position: { latitude: 51.8971, longitude: -8.4828 },
    height_m: 8,
    boresight_deg: 270,
    enabled: false,
  },
];

function track(overrides: Partial<TrackView> = {}): TrackView {
  return {
    track_id: 'TRK-00001',
    state: 'confirmed',
    displayable: true,
    // About 1.1 km north of the reference, 100 m above it.
    position: { latitude: 51.9071, longitude: -8.4697, altitude_m: 160 },
    kinematics: { speed_mps: 20, heading_deg: 180, vertical_rate_mps: 0 },
    classification: { label: 'small_uas', confidence: 0.8, assessed: true },
    threat: {
      risk_score: 84,
      risk_level: 'high',
      reasons: ['On course to enter the protected area in 30 s.'],
      factors: [{ code: 'closure', contribution: 0.42, detail: 'Closing on the site at 20 m/s' }],
      recommendation: 'ESCALATE',
      assessed: true,
    },
    tracking: { contributing_sensors: ['radar_north'] },
    ...overrides,
  };
}

describe('buildSite', () => {
  const site = buildSite(SITE, SENSORS);

  it('anchors the frame at the Aetos site reference', () => {
    expect(site.id).toBe('test-site');
    expect(site.datum).toEqual({ latitude: REF.latitude, longitude: REF.longitude });
  });

  it('turns zones into volumes measured from the site reference', () => {
    const core = site.zones.find((z) => z.id === 'core')!;
    expect(core.zoneClass).toBe('critical');
    expect(core.floor).toBe(0);
    expect(core.ceiling).toBe(UNBOUNDED_CEILING_M);
    expect(Math.hypot(core.polygon[0]!.e, core.polygon[0]!.n)).toBeCloseTo(500, 0);

    const east = site.zones.find((z) => z.id === 'east')!;
    expect(east.zoneClass).toBe('restricted');
    expect(east.floor).toBe(0);
    expect(east.ceiling).toBe(200);
  });

  it('normalises every zone polygon to counter-clockwise', () => {
    for (const zone of site.zones) {
      let area = 0;
      const p = zone.polygon;
      for (let i = 0; i < p.length; i++) {
        const a = p[i]!;
        const b = p[(i + 1) % p.length]!;
        area += a.e * b.n - b.e * a.n;
      }
      expect(area).toBeGreaterThan(0);
    }
  });

  it('keeps enabled sensors only, with the backend coverage', () => {
    expect(site.sensors.map((s) => s.id)).toEqual(['radar_north']);
    const radar = site.sensors[0]!;
    expect(radar.type).toBe('radar');
    expect(radar.coverage.maxRange).toBe(8000);
    expect(radar.coverage.azimuthWidth).toBeCloseTo(Math.PI * 2);
    expect(radar.position.n).toBeGreaterThan(1100);
  });
});

describe('toTrack', () => {
  const site = buildSite(SITE, SENSORS);

  it('places the track in the local frame and keeps the backend assessment', () => {
    const t = toTrack(track(), site, REF.altitude_m, [], 0)!;
    expect(t.position.n).toBeCloseTo(1112, -1);
    expect(Math.abs(t.position.e)).toBeLessThan(1);
    expect(t.altitude).toBeCloseTo(100);
    expect(t.threat.score).toBe(84);
    expect(t.threat.level).toBe('high');
    expect(t.threat.factors[0]).toEqual({ code: 'closure', label: 'Closing on the site at 20 m/s', contribution: 42 });
    expect(t.classification).toBe('uav');
    expect(t.contributingSensors).toEqual(['radar_north']);
  });

  it('heading south toward the site reads as approaching, and predicts entry into the core', () => {
    const t = toTrack(track(), site, REF.altitude_m, [], 0)!;
    expect(t.behaviour).toBe('approaching');
    expect(t.velocity.n).toBeCloseTo(-20);
    expect(t.threat.predictedZoneId).toBe('core');
    // 1112 m out, 500 m radius, 20 m/s: about 30 s, at the 3 s prediction step.
    expect(t.threat.etaToProtectedZone).toBeGreaterThan(27);
    expect(t.threat.etaToProtectedZone).toBeLessThan(36);
  });

  it('drops tracks the console would not show', () => {
    expect(toTrack(track({ state: 'lost' }), site, REF.altitude_m, [], 0)).toBeNull();
    expect(toTrack(track({ displayable: false }), site, REF.altitude_m, [], 0)).toBeNull();
  });

  it('shows an unassessed track as unassessed, not as low risk with a score', () => {
    const t = toTrack(
      track({ threat: { ...track().threat, assessed: false }, classification: { label: 'unknown', confidence: 0, assessed: false } }),
      site,
      REF.altitude_m,
      [],
      0,
    )!;
    expect(t.threat.score).toBe(0);
    expect(t.classification).toBe('unknown');
    expect(adviceFor(track({ threat: { ...track().threat, assessed: false } })).rationale).toMatch(/Not yet assessed/);
  });
});

describe('classify', () => {
  it('maps Aetos labels to the twin vocabulary', () => {
    expect(classify('medium_uas', 0.9)).toBe('uav');
    expect(classify('medium_uas', 0.4)).toBe('probable_uav');
    expect(classify('unknown_uas', 0.9)).toBe('probable_uav');
    expect(classify('bird', 0.9)).toBe('bird');
    expect(classify('vehicle', 0.9)).toBe('unknown');
  });
});

describe('LiveSource.publish', () => {
  it('builds a snapshot with zone emphasis, the backend advice and a growing trail', () => {
    const site = buildSite(SITE, SENSORS);
    const published: Snapshot[] = [];
    const source = new LiveSource({ onSite: () => undefined, onStatus: () => undefined });
    source.primeForTest(site, REF.altitude_m, (s) => published.push(s));

    source.publish([track()], 1000);
    const second = source.publish([track({ position: { ...track().position, latitude: 51.9061 } })], 2000)!;

    expect(published).toHaveLength(2);
    expect(second.seq).toBe(2);
    expect(second.tracks[0]!.history).toHaveLength(2);
    expect(second.zones.core!.predictedIntruderTrackIds).toEqual(['TRK-00001']);
    expect(second.zones.core!.emphasis).toBeGreaterThan(0);
    expect(second.recommendations[0]).toMatchObject({ category: 'escalate' });
    expect(second.weather.label).toBe('Not measured');
    expect(second.coverage.cells.length).toBeGreaterThan(0);
  });

  it('forgets a track that leaves the picture', () => {
    const source = new LiveSource({ onSite: () => undefined, onStatus: () => undefined });
    source.primeForTest(buildSite(SITE, SENSORS), REF.altitude_m, () => undefined);
    source.publish([track()], 1000);
    const empty = source.publish([], 2000)!;
    expect(empty.tracks).toEqual([]);
    const back = source.publish([track()], 3000)!;
    expect(back.tracks[0]!.history).toHaveLength(1);
  });
});

describe('LiveSource lifecycle', () => {
  function respond(map: Record<string, { status: number; body?: unknown }>) {
    return async (input: string) => {
      const path = input.replace('/api/v1', '');
      const entry = map[path] ?? { status: 404 };
      return new Response(JSON.stringify(entry.body ?? {}), { status: entry.status });
    };
  }

  it('reports a signed-out visitor as unauthorised, so the twin can say "sign in"', async () => {
    const statuses: string[] = [];
    let unauthorised = false;
    const source = new LiveSource(
      {
        onSite: () => undefined,
        onStatus: (s) => {
          statuses.push(s.state);
          if (s.state === 'failed') unauthorised = s.unauthorised;
        },
      },
      respond({ '/sites': { status: 401 }, '/sensors': { status: 401 } }),
    );
    source.start(() => undefined);
    await new Promise((r) => setTimeout(r, 10));
    source.stop();
    expect(statuses).toEqual(['connecting', 'failed']);
    expect(unauthorised).toBe(true);
  });

  it('loads the site, then publishes tracks', async () => {
    let site: string | null = null;
    const snapshots: Snapshot[] = [];
    const source = new LiveSource(
      { onSite: (s) => (site = s.id), onStatus: () => undefined },
      respond({
        '/sites': { status: 200, body: { sites: [SITE] } },
        '/sensors': { status: 200, body: { sensors: SENSORS } },
        '/tracks': { status: 200, body: { tracks: [track()] } },
      }),
      10_000,
    );
    source.start((s) => snapshots.push(s));
    await new Promise((r) => setTimeout(r, 20));
    source.stop();
    expect(site).toBe('test-site');
    expect(snapshots[0]!.tracks[0]!.trackId).toBe('TRK-00001');
  });
});

describe('wide monitored zones', () => {
  const wide: SiteView = {
    ...SITE,
    protected_radius_m: 800,
    zones: [
      ...SITE.zones,
      {
        zone_id: 'outer',
        name: 'Outer monitored',
        kind: 'monitored',
        geometry: { shape: 'circle', centre: { latitude: REF.latitude, longitude: REF.longitude }, radius_m: 8000 },
        floor_m: null,
        ceiling_m: null,
      },
    ],
  };

  it('do not size the scene, which is framed on the protected area', () => {
    const site = buildSite(wide, SENSORS);
    const radius = Math.max(...site.boundary.map((p) => Math.hypot(p.e, p.n)));
    expect(radius).toBeCloseTo(1600, 0);
  });

  it('do not count as a protected volume about to be entered', () => {
    const site = buildSite(wide, SENSORS);
    // Heading away from the site: it is inside the 8 km monitored zone but entering nothing.
    const t = toTrack(track({ kinematics: { speed_mps: 20, heading_deg: 0, vertical_rate_mps: 0 } }), site, REF.altitude_m, [], 0)!;
    expect(t.threat.predictedZoneId).toBeUndefined();
    expect(t.threat.etaToProtectedZone).toBeUndefined();
  });
});

describe('liveHealth', () => {
  it('does not mark the site down for response resources Aetos never reports', async () => {
    const { liveHealth } = await import('./LiveSource');
    const site = buildSite(SITE, SENSORS);
    const health = liveHealth(site, { resolution: 100, extent: 100, cells: [{ e: 0, n: 0, value: 1, quality: 'strong' }], weakestSector: 'north' });
    expect(health.readiness).toBeCloseTo(1);
    expect(health.subsystems.map((s) => s.id)).not.toContain('RESPONSE');
    expect(health.primaryWeakness).toBeUndefined();
  });
});
