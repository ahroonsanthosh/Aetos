/**
 * Engine tests.
 *
 * The engine's contract with everything downstream is the `Snapshot`: a complete,
 * self-consistent picture at one instant, emitted at a fixed rate, containing nothing that
 * identifies it as simulated. These tests hold that contract, because it is the thing that
 * makes the eventual swap to Aetos Core a change of one line rather than a rewrite.
 */

import { describe, expect, it } from 'vitest';

import { DEMO_SITE } from '@/site/demo-site';
import { SimulationEngine, TICK_HZ } from './engine';
import { SCENARIOS } from './scenarios';

function run(scenarioId: string, seconds: number) {
  const engine = new SimulationEngine(DEMO_SITE, scenarioId);
  const dt = 1 / TICK_HZ;
  let snapshot = engine.tick(0);
  for (let t = 0; t < seconds * TICK_HZ; t++) {
    snapshot = engine.tick(dt);
  }
  return { engine, snapshot };
}

describe('SimulationEngine', () => {
  it('produces a complete snapshot on the very first tick', () => {
    const engine = new SimulationEngine(DEMO_SITE, 'critical-approach');
    const snapshot = engine.tick(0);

    expect(snapshot.seq).toBe(1);
    expect(snapshot.coverage.cells.length).toBeGreaterThan(0);
    expect(snapshot.health.readiness).toBeGreaterThan(0);
    expect(Object.keys(snapshot.assets)).toHaveLength(DEMO_SITE.assets.length);
    expect(Object.keys(snapshot.sensors)).toHaveLength(DEMO_SITE.sensors.length);
    expect(Object.keys(snapshot.zones)).toHaveLength(DEMO_SITE.zones.length);
  });

  it('increments the sequence number monotonically', () => {
    const engine = new SimulationEngine(DEMO_SITE, 'single-uav');
    const seqs = Array.from({ length: 5 }, () => engine.tick(0.1).seq);
    expect(seqs).toEqual([1, 2, 3, 4, 5]);
  });

  it('is deterministic — the same scenario run twice gives the same picture', () => {
    const a = run('critical-approach', 30).snapshot;
    const b = run('critical-approach', 30).snapshot;

    expect(a.tracks.map((t) => t.trackId)).toEqual(b.tracks.map((t) => t.trackId));
    expect(a.tracks.map((t) => Math.round(t.position.e))).toEqual(
      b.tracks.map((t) => Math.round(t.position.e)),
    );
    expect(a.tracks.map((t) => t.threat.score)).toEqual(b.tracks.map((t) => t.threat.score));
  });

  it('leaks nothing that identifies a track as simulated', () => {
    const { snapshot } = run('multiple', 20);
    const serialised = JSON.stringify(snapshot.tracks);

    // The renderer must not be able to tell where a detection came from — the governing
    // principle of the wider Aetos system, applied here at the transport boundary.
    for (const tell of ['plan', 'waypoint', 'simulat', 'scenario', 'startDelay', 'loop']) {
      expect(serialised.toLowerCase()).not.toContain(tell.toLowerCase());
    }
  });

  it('never recommends anything beyond operator review', () => {
    const permitted = new Set(['monitor', 'verify', 'alert', 'escalate', 'operator_review_required']);

    for (const scenario of SCENARIOS) {
      const { snapshot } = run(scenario.id, 25);
      for (const recommendation of snapshot.recommendations) {
        expect(permitted.has(recommendation.category)).toBe(true);
      }
    }
  });

  it('builds track history and a forward prediction', () => {
    const { snapshot } = run('critical-approach', 12);
    const track = snapshot.tracks[0]!;

    expect(track.history.length).toBeGreaterThan(1);
    expect(track.predicted.length).toBeGreaterThan(1);
    // The prediction leads the object; the history trails it.
    expect(track.predicted[0]!.t).toBeGreaterThan(snapshot.t);
    expect(track.history[track.history.length - 1]!.t).toBeLessThanOrEqual(snapshot.t);
  });

  it('applies scenario sensor failures to the sensor state', () => {
    const { snapshot } = run('coverage-gap', 5);
    expect(snapshot.sensors['RADAR-02']!.status).toBe('offline');
    expect(snapshot.sensors['RADAR-02']!.effectiveness).toBe(0);
    expect(snapshot.health.primaryWeakness).toBeTruthy();
  });

  it('degrades optical sensors in fog and leaves radar alone', () => {
    const { snapshot } = run('weather', 5);
    expect(snapshot.weather.condition).toBe('fog');

    const camera = snapshot.sensors['CAM-01']!;
    const radar = snapshot.sensors['RADAR-01']!;

    expect(camera.effectiveness).toBeLessThan(radar.effectiveness);
    expect(camera.status).toBe('degraded');
    expect(radar.status).toBe('online');
  });

  it('holds a paused simulation still without stopping the feed', () => {
    const engine = new SimulationEngine(DEMO_SITE, 'critical-approach');
    engine.tick(0.1);
    const before = engine.tick(0);
    const after = engine.tick(0);

    // Sequence still advances — a frozen feed and a stalled source must never look alike.
    expect(after.seq).toBeGreaterThan(before.seq);
    expect(after.tracks[0]!.position.e).toBeCloseTo(before.tracks[0]!.position.e, 6);
  });

  it('resets cleanly when a scenario is loaded', () => {
    const engine = new SimulationEngine(DEMO_SITE, 'multiple');
    for (let i = 0; i < 100; i++) engine.tick(0.1);
    expect(engine.tick(0.1).tracks.length).toBeGreaterThan(1);

    engine.loadScenario('idle');
    const snapshot = engine.tick(0.1);
    expect(snapshot.tracks).toHaveLength(0);
    expect(engine.scenarioId).toBe('idle');
  });

  it('keeps every scenario running without producing a broken picture', () => {
    for (const scenario of SCENARIOS) {
      const { snapshot } = run(scenario.id, 40);

      for (const track of snapshot.tracks) {
        expect(Number.isFinite(track.position.e)).toBe(true);
        expect(Number.isFinite(track.position.n)).toBe(true);
        expect(track.altitude).toBeGreaterThan(0);
        expect(track.threat.score).toBeGreaterThanOrEqual(0);
        expect(track.threat.score).toBeLessThanOrEqual(100);
        expect(track.confidence).toBeGreaterThan(0);
        expect(track.confidence).toBeLessThanOrEqual(1);
      }

      expect(snapshot.health.readiness).toBeGreaterThanOrEqual(0);
      expect(snapshot.health.readiness).toBeLessThanOrEqual(1);
    }
  });

  it('drives the substation zone hard during the critical-approach run', () => {
    // Sampled across the run rather than at one instant: the object orbits the substation,
    // so the moment of peak emphasis depends on where it is in the pattern.
    const engine = new SimulationEngine(DEMO_SITE, 'critical-approach');
    let peak = 0;
    for (let t = 0; t < 120 * TICK_HZ; t++) {
      const snapshot = engine.tick(1 / TICK_HZ);
      peak = Math.max(peak, snapshot.zones['ZONE-CRIT-SUB']!.emphasis);
    }
    expect(peak).toBeGreaterThan(0.6);
  });

  it('leaves the substation zone quiet when only birds are about', () => {
    const engine = new SimulationEngine(DEMO_SITE, 'false-positive');
    let peak = 0;
    for (let t = 0; t < 90 * TICK_HZ; t++) {
      const snapshot = engine.tick(1 / TICK_HZ);
      peak = Math.max(peak, snapshot.zones['ZONE-CRIT-SUB']!.emphasis);
    }
    // A flock directly overhead may register as present, but must never read as a threat.
    expect(peak).toBeLessThan(0.4);
  });
});
