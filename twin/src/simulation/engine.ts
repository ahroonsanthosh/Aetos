/**
 * The simulation engine.
 *
 * Produces `Snapshot`s at a fixed rate, exactly as a WebSocket from Aetos Core would. It
 * knows about the site and the scenario; it knows nothing about React, three.js, or the
 * fact that anything is being drawn.
 *
 * The rate is deliberately far below frame rate. A simulator that produced a new position
 * every frame would let the renderer become dependent on frame-rate data, and the swap to
 * a real 10 Hz feed would then look broken. Interpolating between snapshots is the
 * renderer's job, and it is a job it has to do either way.
 */

import type {
  Asset,
  Recommendation,
  Sensor,
  Site,
  Snapshot,
  ThreatLevel,
  Track,
  TrackPoint,
  Weather,
  Zone,
} from '@/types';
import { clamp01, horizontalDistance, pointInVolume } from '@/lib/geometry';
import {
  assetProtectionScore,
  buildCoverageGrid,
  buildSystemHealth,
  sensorsCovering,
} from './coverage';
import { deriveBehaviour, extrapolate, flightStateAt } from './flight';
import { assessThreat } from './threat';
import { DEFAULT_SCENARIO_ID, scenarioById, type Scenario } from './scenarios';
import { WEATHER } from './weather';

/** Snapshot rate. Matches the cadence a real Core feed would plausibly publish at. */
export const TICK_HZ = 10;
export const TICK_MS = 1000 / TICK_HZ;

/** Coverage grid parameters. Coarse enough to recompute cheaply, fine enough to read. */
const COVERAGE_EXTENT = 700;
const COVERAGE_RESOLUTION = 50;

/**
 * Track history: one point per second of *simulated* time, bounded.
 *
 * Measured on the simulation clock rather than the wall clock. Gating on wall time ties
 * the length of a track's trail to how fast the scenario happens to be playing — at 4×
 * the trail covers a quarter of the ground, and a throttled background tab stops
 * recording history altogether while the objects keep moving.
 */
const HISTORY_INTERVAL_SECONDS = 1;
const HISTORY_LIMIT = 70;

interface TrackRuntime {
  history: TrackPoint[];
  /** Simulation elapsed time, seconds, when the last history point was taken. */
  lastHistoryAt: number;
  previousLevel?: ThreatLevel;
}

export class SimulationEngine {
  private readonly site: Site;
  private scenario: Scenario;
  private elapsed = 0;
  private seq = 0;
  private readonly runtime = new Map<string, TrackRuntime>();

  /**
   * Coverage, protection and health depend only on sensor and resource state, which
   * changes on scenario load rather than on every tick. Recomputing the grid ten times a
   * second would be pure waste, so it is cached against a signature of that state.
   */
  private cache?: {
    signature: string;
    sensors: Sensor[];
    coverage: ReturnType<typeof buildCoverageGrid>;
    protection: Record<string, number>;
    covering: Record<string, string[]>;
  };

  constructor(site: Site, scenarioId: string = DEFAULT_SCENARIO_ID) {
    this.site = site;
    this.scenario = scenarioById(scenarioId);
  }

  get scenarioId(): string {
    return this.scenario.id;
  }

  /** Restart with a different scenario. Elapsed time and all track history reset. */
  loadScenario(id: string): void {
    this.scenario = scenarioById(id);
    this.elapsed = 0;
    this.runtime.clear();
    this.cache = undefined;
  }

  /** Advance by `dt` seconds and produce the picture at that instant. */
  tick(dt: number, nowMs: number = Date.now()): Snapshot {
    this.elapsed += dt;
    this.seq += 1;

    const weather = WEATHER[this.scenario.weather];
    const { sensors, coverage, protection, covering } = this.resolveStatic(weather);

    const tracks = this.buildTracks(nowMs, sensors);

    // --- Zones -------------------------------------------------------------
    const zoneState: Snapshot['zones'] = {};
    for (const zone of this.site.zones) {
      zoneState[zone.id] = this.assessZone(zone, tracks);
    }

    // --- Assets ------------------------------------------------------------
    const assetState: Snapshot['assets'] = {};
    for (const asset of this.site.assets) {
      const threatening = tracks
        .filter((t) => t.threat.threatenedAssetId === asset.id && t.threat.level !== 'low')
        .map((t) => t.trackId);
      assetState[asset.id] = {
        status: asset.status,
        activeThreats: threatening,
        protectionScore: protection[asset.id] ?? asset.protectionScore,
      };
    }

    // --- Sensors -----------------------------------------------------------
    const sensorState: Snapshot['sensors'] = {};
    for (const sensor of sensors) {
      sensorState[sensor.id] = {
        status: sensor.status,
        health: sensor.health,
        effectiveness: sensor.effectiveness,
        degradationReason: sensor.degradationReason,
      };
    }

    // --- Health ------------------------------------------------------------
    const assetsForHealth: Asset[] = this.site.assets.map((a) => ({
      ...a,
      protectionScore: protection[a.id] ?? a.protectionScore,
      sensorsCovering: covering[a.id] ?? [],
    }));
    const health = buildSystemHealth(sensors, coverage, this.site.resources, weather, assetsForHealth);

    return {
      t: nowMs,
      seq: this.seq,
      tracks,
      zones: zoneState,
      assets: assetState,
      sensors: sensorState,
      coverage,
      health,
      weather,
      recommendations: tracks.map(recommend),
    };
  }

  /** Sensors that this asset's coverage is currently drawn from. */
  sensorsCoveringAsset(assetId: string): string[] {
    return this.cache?.covering[assetId] ?? [];
  }

  // -------------------------------------------------------------------------

  /**
   * Apply scenario overrides and weather to the site's sensors, then derive everything
   * that depends only on them. Cached — see the field comment.
   */
  private resolveStatic(weather: Weather) {
    const signature = `${this.scenario.id}|${weather.condition}`;
    if (this.cache?.signature === signature) return this.cache;

    const sensors: Sensor[] = this.site.sensors.map((base) => {
      const override = this.scenario.sensorOverrides[base.id];
      const status = override?.status ?? base.status;
      const health = override?.health ?? base.health;
      const impact = weather.sensorImpact[base.type] ?? 1;
      const effectiveness = status === 'offline' ? 0 : clamp01(health * impact);

      const reason =
        override?.reason ??
        (impact < 1 ? `${weather.label} reducing ${base.type} performance` : undefined);

      // A sensor at less than 60% of its own health is reported degraded even when the
      // cause is the weather rather than the device. The operator cares about capability.
      const derivedStatus =
        status === 'offline' ? 'offline' : effectiveness < health * 0.6 ? 'degraded' : status;

      return { ...base, status: derivedStatus, health, effectiveness, degradationReason: reason };
    });

    const coverage = buildCoverageGrid(sensors, COVERAGE_EXTENT, COVERAGE_RESOLUTION);

    const protection: Record<string, number> = {};
    const covering: Record<string, string[]> = {};
    for (const asset of this.site.assets) {
      protection[asset.id] = assetProtectionScore(asset, sensors, this.site.resources);
      covering[asset.id] = sensorsCovering(sensors, {
        ...asset.position,
        u: asset.position.u + 60,
      });
    }

    this.cache = { signature, sensors, coverage, protection, covering };
    return this.cache;
  }

  private buildTracks(nowMs: number, sensors: Sensor[]): Track[] {
    const tracks: Track[] = [];

    // Counted first, because simultaneous UAV activity is an input to every score.
    const uavLike = this.scenario.flights.filter(
      (f) =>
        (f.classification === 'uav' || f.classification === 'probable_uav') &&
        f.identity !== 'authorised' &&
        this.elapsed >= f.startDelay,
    ).length;

    for (const flight of this.scenario.flights) {
      const state = flightStateAt(flight, this.elapsed);
      if (!state || state.finished) {
        this.runtime.delete(flight.id);
        continue;
      }

      let rt = this.runtime.get(flight.id);
      if (!rt) {
        // Negative infinity so the first tick always records a point, rather than the
        // track appearing with an empty trail for its first simulated second.
        rt = { history: [], lastHistoryAt: -Infinity };
        this.runtime.set(flight.id, rt);
      }

      if (this.elapsed - rt.lastHistoryAt >= HISTORY_INTERVAL_SECONDS) {
        rt.history.push({ position: state.position, t: nowMs });
        if (rt.history.length > HISTORY_LIMIT) rt.history.shift();
        rt.lastHistoryAt = this.elapsed;
      }

      const predicted = extrapolate(state.position, state.velocity, nowMs);
      const behaviour = deriveBehaviour(flight, state.position, state.velocity);

      // How well the sensors named on this plan are actually performing right now. A
      // track held by a fogged-out camera is a track the system is less sure about.
      const contributing = sensors.filter((s) => flight.contributingSensors.includes(s.id));
      const online = contributing.filter((s) => s.status !== 'offline');
      const sensorConfidence = online.length
        ? online.reduce((sum, s) => sum + s.effectiveness, 0) / online.length
        : 0.2;

      const threat = assessThreat({
        position: state.position,
        speed: state.speed,
        classification: flight.classification,
        confidence: flight.confidence * clamp01(0.55 + sensorConfidence * 0.45),
        identity: flight.identity,
        behaviour,
        predicted,
        now: nowMs,
        assets: this.site.assets,
        zones: this.site.zones,
        simultaneousUavCount: uavLike,
        sensorConfidence,
        previousLevel: rt.previousLevel,
      });
      rt.previousLevel = threat.level;

      tracks.push({
        trackId: flight.id,
        classification: flight.classification,
        confidence: flight.confidence * clamp01(0.55 + sensorConfidence * 0.45),
        position: state.position,
        altitude: state.position.u,
        velocity: state.velocity,
        speed: state.speed,
        heading: state.heading,
        range: horizontalDistance(state.position, { e: 0, n: 0, u: 0 }),
        behaviour,
        identity: flight.identity,
        status: sensorConfidence < 0.3 ? 'coasting' : 'confirmed',
        threat,
        history: rt.history.slice(),
        predicted,
        contributingSensors: online.map((s) => s.id),
      });
    }

    return tracks;
  }

  /**
   * Zone emphasis: how strongly the volume should assert itself.
   *
   * An occupied critical zone goes to full. Otherwise emphasis follows the worst threat
   * predicted to enter it, and decays with how far away in time that entry is — so a zone
   * begins to show before anything reaches it, which is the entire point of predicting.
   */
  private assessZone(zone: Zone, tracks: Track[]): Snapshot['zones'][string] {
    const intruderTrackIds: string[] = [];
    const predictedIntruderTrackIds: string[] = [];
    let emphasis = 0;

    for (const track of tracks) {
      const inside = pointInVolume(track.position, zone.polygon, zone.floor, zone.ceiling);
      if (inside) {
        intruderTrackIds.push(track.trackId);
        emphasis = Math.max(emphasis, track.threat.level === 'low' ? 0.35 : 1);
      }

      if (track.threat.predictedZoneId === zone.id) {
        predictedIntruderTrackIds.push(track.trackId);

        // The window is deliberately short. A zone that brightens for anything merely
        // predicted to reach it eventually is lit most of the time, and a permanently lit
        // zone carries no information — it just sits over the asset it is meant to draw
        // attention to. Ninety seconds out is not yet news; thirty seconds is.
        const eta = track.threat.etaToProtectedZone ?? 999;
        const imminence = clamp01(1 - eta / 75);
        const severity =
          track.threat.level === 'critical'
            ? 1
            : track.threat.level === 'high'
              ? 0.75
              : track.threat.level === 'medium'
                ? 0.35
                : 0.12;
        emphasis = Math.max(emphasis, imminence * severity);
      }
    }

    return { emphasis, intruderTrackIds, predictedIntruderTrackIds };
  }
}

/**
 * Turn an assessment into advice.
 *
 * The ladder tops out at "operator review required". There is no rung above it in this
 * codebase and there must not be one: the next state after a recommendation is a human
 * authorising something, and that is not a state this prototype can author.
 */
function recommend(track: Track): Recommendation {
  const { level, threatenedAssetId, etaToProtectedZone } = track.threat;

  if (level === 'critical') {
    return {
      trackId: track.trackId,
      category: 'operator_review_required',
      rationale: threatenedAssetId
        ? `Unresolved track assessed against ${threatenedAssetId}. Operator decision required.`
        : 'Unresolved high-severity track. Operator decision required.',
    };
  }

  if (level === 'high') {
    return {
      trackId: track.trackId,
      category: 'escalate',
      rationale:
        etaToProtectedZone !== undefined
          ? `Protected volume entry predicted in ${Math.round(etaToProtectedZone)} s. Escalate for review.`
          : 'Sustained high assessment. Escalate for review.',
    };
  }

  if (level === 'medium') {
    return {
      trackId: track.trackId,
      category: track.identity === 'unknown' ? 'verify' : 'alert',
      rationale:
        track.identity === 'unknown'
          ? 'Identity unresolved. Attempt visual identification before escalating.'
          : 'Known object behaving atypically. Hold under alert.',
    };
  }

  return {
    trackId: track.trackId,
    category: 'monitor',
    rationale: 'No protected asset threatened on the current trajectory. Continue monitoring.',
  };
}
