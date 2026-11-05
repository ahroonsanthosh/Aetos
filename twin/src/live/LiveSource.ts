/**
 * The live feed: the Aetos console's own site, sensors and tracks, as twin snapshots.
 *
 * On start it reads the site and its sensors once and hands the twin a `Site` built from them,
 * so the scene shows the real protected zones and sensor placements rather than the demo
 * facility. Then it polls the tracks once a second and publishes a snapshot each time. The
 * renderer interpolates between snapshots, so a one-second cadence still moves smoothly.
 *
 * Failures are reported, never hidden: a 401 means "sign in to the console", anything else
 * means the backend is unreachable or broken, and the twin falls back to its simulation with
 * the reason on screen. A poll that fails after the feed is up keeps the last picture and
 * retries; the status says the feed is interrupted.
 */

import type { CoverageGrid, Site, Snapshot, SystemHealth, TrackPoint, Weather } from '@/types';
import { buildCoverageGrid } from '@/simulation/coverage';
import { assessZone } from '@/simulation/engine';
import { deriveScenePresentation } from '@/site/presentation';
import type { DataSource } from '@/state/source';
import { getJson, LiveFeedError, type Fetch, type SensorView, type SiteView, type TrackView } from './api';
import { adviceFor, buildSite, toTrack } from './convert';

export type LiveStatus =
  | { state: 'connecting' }
  | { state: 'live' }
  | { state: 'interrupted'; reason: string }
  | { state: 'failed'; reason: string; unauthorised: boolean };

export interface LiveHooks {
  onSite(site: Site): void;
  onStatus(status: LiveStatus): void;
}

export const POLL_MS = 1000;
const HISTORY_LIMIT = 70;

/** Aetos reports no weather; the twin says so rather than claiming clear skies. */
const UNMEASURED: Weather = { condition: 'clear', label: 'Not measured', sensorImpact: {} };

export class LiveSource implements DataSource {
  readonly kind = 'live' as const;

  private listener?: (snapshot: Snapshot) => void;
  private abort?: AbortController;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private seq = 0;
  private site: Site | null = null;
  private refAlt = 0;
  private history = new Map<string, TrackPoint[]>();
  private staticPart: Pick<Snapshot, 'coverage' | 'health' | 'sensors'> | null = null;

  constructor(
    private readonly hooks: LiveHooks,
    private readonly fetchImpl: Fetch = (input, init) => fetch(input, init),
    private readonly pollMs = POLL_MS,
  ) {}

  start(onSnapshot: (snapshot: Snapshot) => void): void {
    if (this.abort) return;
    this.listener = onSnapshot;
    this.abort = new AbortController();
    this.hooks.onStatus({ state: 'connecting' });
    void this.connect(this.abort.signal);
  }

  stop(): void {
    this.abort?.abort();
    this.abort = undefined;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.listener = undefined;
  }

  private async connect(signal: AbortSignal): Promise<void> {
    try {
      const [{ sites }, { sensors }] = await Promise.all([
        getJson<{ sites: SiteView[] }>(this.fetchImpl, '/sites', signal),
        getJson<{ sensors: SensorView[] }>(this.fetchImpl, '/sensors', signal),
      ]);
      const view = sites[0];
      if (!view) throw new LiveFeedError(404, 'The Aetos backend has no site configured.');
      if (signal.aborted) return;

      this.site = buildSite(
        view,
        sensors.filter((s) => s.site_id === view.site_id),
      );
      this.refAlt = view.reference.altitude_m;
      this.staticPart = this.buildStatic(this.site);
      this.hooks.onSite(this.site);
      await this.poll(signal);
    } catch (cause) {
      if (signal.aborted) return;
      const error = cause instanceof LiveFeedError ? cause : new LiveFeedError(0, String(cause));
      this.hooks.onStatus({ state: 'failed', reason: error.message, unauthorised: error.status === 401 });
    }
  }

  private async poll(signal: AbortSignal): Promise<void> {
    if (signal.aborted) return;
    try {
      const { tracks } = await getJson<{ tracks: TrackView[] }>(this.fetchImpl, '/tracks', signal);
      if (signal.aborted) return;
      this.publish(tracks);
      this.hooks.onStatus({ state: 'live' });
    } catch (cause) {
      if (signal.aborted) return;
      this.hooks.onStatus({
        state: 'interrupted',
        reason: cause instanceof Error ? cause.message : String(cause),
      });
    }
    this.timer = setTimeout(() => void this.poll(signal), this.pollMs);
  }

  /** Assemble and publish one snapshot from a page of backend tracks. */
  publish(views: TrackView[], nowMs: number = Date.now()): Snapshot | null {
    const site = this.site;
    const fixed = this.staticPart;
    if (!site || !fixed) return null;

    const seen = new Set<string>();
    const tracks = [];
    const recommendations = [];
    for (const view of views) {
      const history = this.history.get(view.track_id) ?? [];
      const track = toTrack(view, site, this.refAlt, history, nowMs);
      if (!track) continue;
      seen.add(track.trackId);
      history.push({ position: track.position, t: nowMs });
      if (history.length > HISTORY_LIMIT) history.shift();
      this.history.set(track.trackId, history);
      tracks.push({ ...track, history: history.slice() });
      recommendations.push(adviceFor(view));
    }
    for (const id of [...this.history.keys()]) if (!seen.has(id)) this.history.delete(id);

    const zones: Snapshot['zones'] = {};
    for (const zone of site.zones) zones[zone.id] = assessZone(zone, tracks);

    this.seq += 1;
    const snapshot: Snapshot = {
      t: nowMs,
      seq: this.seq,
      tracks,
      zones,
      assets: {},
      sensors: fixed.sensors,
      coverage: fixed.coverage,
      health: fixed.health,
      weather: UNMEASURED,
      recommendations,
    };
    this.listener?.(snapshot);
    return snapshot;
  }

  /** Coverage and health depend only on the sensors, which do not change while the feed runs. */
  private buildStatic(site: Site): Pick<Snapshot, 'coverage' | 'health' | 'sensors'> {
    const presentation = deriveScenePresentation(site);
    const coverage = buildCoverageGrid(
      site.sensors,
      presentation.coverageExtent,
      presentation.coverageResolution,
    );
    const sensors: Snapshot['sensors'] = {};
    for (const s of site.sensors) {
      sensors[s.id] = { status: s.status, health: s.health, effectiveness: s.effectiveness };
    }
    return { coverage, sensors, health: liveHealth(site, coverage) };
  }

  /** For tests: drive the source with a site already built. */
  primeForTest(site: Site, refAlt: number, listener: (s: Snapshot) => void): void {
    this.site = site;
    this.refAlt = refAlt;
    this.staticPart = this.buildStatic(site);
    this.listener = listener;
  }
}

/**
 * Readiness from what Aetos actually knows: its sensors and the coverage they give.
 *
 * The simulator's readiness also weighs response resources and their communications, which
 * Aetos does not report. Leaving them in would score the live site down for "no resources" it
 * never claimed to lack, so they are left out rather than invented.
 */
export function liveHealth(site: Site, coverage: CoverageGrid): SystemHealth {
  const mean = coverage.cells.reduce((sum, c) => sum + c.value, 0) / Math.max(coverage.cells.length, 1);
  const sensorMean = site.sensors.length
    ? site.sensors.reduce((sum, s) => sum + (s.status === 'offline' ? 0 : s.effectiveness), 0) /
      site.sensors.length
    : 0;
  const gaps = coverage.cells.filter((c) => c.quality === 'gap').length / Math.max(coverage.cells.length, 1);

  return {
    readiness: Math.min(1, Math.max(0, mean * 0.58 + sensorMean * 0.42)),
    subsystems: [
      ...site.sensors.map((s) => ({
        id: s.id,
        label: s.name,
        value: s.effectiveness,
        status: s.status === 'offline' ? ('offline' as const) : s.status === 'degraded' ? ('degraded' as const) : ('nominal' as const),
      })),
      {
        id: 'COVERAGE',
        label: 'Site coverage',
        value: mean,
        status: mean > 0.6 ? ('nominal' as const) : mean > 0.35 ? ('degraded' as const) : ('offline' as const),
      },
    ],
    primaryWeakness: site.sensors.length === 0
      ? 'No sensors are configured at this site.'
      : gaps > 0.28
        ? `Detection gap across the ${coverage.weakestSector} sector.`
        : undefined,
    recommendedAction: gaps > 0.28 ? 'Review sensor siting for the weak sector.' : undefined,
  };
}
