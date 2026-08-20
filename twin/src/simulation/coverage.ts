/**
 * Simulated coverage, protection and readiness.
 *
 * The question this exists to answer is the one a customer actually asks:
 *
 *   "Where am I vulnerable?"
 *
 * A number alone cannot answer it, so everything here produces a spatial answer first and
 * a scalar second. The scalars are derived from the map, never asserted independently of
 * it — which is why killing a sensor moves the readiness figure, the coverage colours and
 * the affected assets' protection scores together, as one consequence rather than three.
 */

import type {
  Asset,
  CoverageCell,
  CoverageGrid,
  CoverageQuality,
  Enu,
  ResponseResource,
  Sensor,
  SubsystemHealth,
  SystemHealth,
  Weather,
} from '@/types';
import { angleDelta, clamp01, horizontalDistance, TAU } from '@/lib/geometry';

/** Altitude the coverage map is evaluated at. A ground map of an airspace sensor is a lie. */
export const COVERAGE_EVAL_ALTITUDE = 60;

const STRONG = 0.62;
const REDUCED = 0.28;

export function qualityFor(value: number): CoverageQuality {
  if (value >= STRONG) return 'strong';
  if (value >= REDUCED) return 'reduced';
  return 'gap';
}

/**
 * How well one sensor sees one point, 0–1.
 *
 * Geometry gates it — outside the sector, too near, too far or too high and the answer is
 * zero. Inside, the value is the sensor's current effectiveness attenuated toward the
 * edges of its envelope, because a target at maximum range is not held as well as one at
 * half range.
 */
export function sensorCoverageAt(sensor: Sensor, point: Enu): number {
  if (sensor.status === 'offline' || sensor.effectiveness <= 0) return 0;

  const head: Enu = { ...sensor.position, u: sensor.position.u + sensor.mastHeight };
  const ground = horizontalDistance(head, point);
  const c = sensor.coverage;

  if (ground < c.minRange || ground > c.maxRange) return 0;

  // Azimuth gate. An omnidirectional sensor skips it.
  if (c.azimuthWidth < TAU - 1e-6) {
    const b = Math.atan2(point.e - head.e, point.n - head.n);
    if (Math.abs(angleDelta(c.bearing, b)) > c.azimuthWidth / 2) return 0;
  }

  // Elevation gate.
  const elevation = Math.atan2(point.u - head.u, Math.max(ground, 1e-3));
  if (elevation < c.minElevation || elevation > c.maxElevation) return 0;

  // Range attenuation: flat across the useful band, tapering over the outer third.
  const span = c.maxRange - c.minRange;
  const through = (ground - c.minRange) / Math.max(span, 1);
  const rangeFalloff = through < 0.66 ? 1 : clamp01(1 - (through - 0.66) / 0.34) * 0.85 + 0.15;

  return sensor.effectiveness * rangeFalloff;
}

/**
 * Aggregate several sensors over one point. Combined as independent probabilities of
 * detection rather than a sum, so three mediocre sensors are better than one and none of
 * them ever exceeds certainty.
 */
export function aggregateCoverageAt(sensors: Sensor[], point: Enu): number {
  let miss = 1;
  for (const sensor of sensors) {
    miss *= 1 - sensorCoverageAt(sensor, point);
  }
  return 1 - miss;
}

/** Sensors that meaningfully contribute at a point, strongest first. */
export function sensorsCovering(sensors: Sensor[], point: Enu, threshold = 0.1): string[] {
  return sensors
    .map((s) => ({ id: s.id, v: sensorCoverageAt(s, point) }))
    .filter((x) => x.v >= threshold)
    .sort((a, b) => b.v - a.v)
    .map((x) => x.id);
}

const COMPASS: { label: string; bearing: number }[] = [
  { label: 'north', bearing: 0 },
  { label: 'north-east', bearing: Math.PI / 4 },
  { label: 'east', bearing: Math.PI / 2 },
  { label: 'south-east', bearing: (3 * Math.PI) / 4 },
  { label: 'south', bearing: Math.PI },
  { label: 'south-west', bearing: (5 * Math.PI) / 4 },
  { label: 'west', bearing: (3 * Math.PI) / 2 },
  { label: 'north-west', bearing: (7 * Math.PI) / 4 },
];

export function buildCoverageGrid(
  sensors: Sensor[],
  extent: number,
  resolution: number,
): CoverageGrid {
  const cells: CoverageCell[] = [];
  const sectorTotals = new Array(COMPASS.length).fill(0);
  const sectorCounts = new Array(COMPASS.length).fill(0);

  for (let n = -extent; n <= extent; n += resolution) {
    for (let e = -extent; e <= extent; e += resolution) {
      const value = aggregateCoverageAt(sensors, { e, n, u: COVERAGE_EVAL_ALTITUDE });
      cells.push({ e, n, value, quality: qualityFor(value) });

      // Attribute the cell to a compass sector for the readiness narrative. Cells near
      // the datum have no meaningful bearing, so they are excluded.
      const r = Math.hypot(e, n);
      if (r < extent * 0.35) continue;
      let b = Math.atan2(e, n);
      if (b < 0) b += TAU;
      const index = Math.round(b / (Math.PI / 4)) % COMPASS.length;
      sectorTotals[index] += value;
      sectorCounts[index] += 1;
    }
  }

  let weakestSector = COMPASS[0]!.label;
  let weakest = Infinity;
  for (let i = 0; i < COMPASS.length; i++) {
    if (sectorCounts[i] === 0) continue;
    const mean = sectorTotals[i] / sectorCounts[i];
    if (mean < weakest) {
      weakest = mean;
      weakestSector = COMPASS[i]!.label;
    }
  }

  return { resolution, extent, cells, weakestSector };
}

/**
 * How well Aetos believes it can protect one asset right now.
 *
 * Detection is necessary but not sufficient: an asset that is perfectly observed but has
 * no available response resource is not fully protected, and the score says so. Weighted
 * so that sensing dominates — seeing nothing is worse than having nothing to send.
 */
export function assetProtectionScore(
  asset: Asset,
  sensors: Sensor[],
  resources: ResponseResource[],
): number {
  // Sample a shell around the asset rather than the asset itself. What matters is whether
  // an approach would be seen coming, not whether the object on the ground is in view.
  const samples: Enu[] = [];
  const radii = [80, 200, 380];
  for (const r of radii) {
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * TAU;
      samples.push({
        e: asset.position.e + Math.sin(a) * r,
        n: asset.position.n + Math.cos(a) * r,
        u: COVERAGE_EVAL_ALTITUDE,
      });
    }
  }

  let detection = 0;
  for (const p of samples) detection += aggregateCoverageAt(sensors, p);
  detection /= samples.length;

  const available = resources.filter((r) => r.status === 'available');
  const responseReadiness = available.length === 0
    ? 0
    : clamp01(available.reduce((s, r) => s + r.readiness, 0) / Math.max(available.length, 1)) *
      clamp01(available.length / 3);

  const comms = resources.length === 0
    ? 1
    : resources.filter((r) => r.communications === 'connected').length / resources.length;

  return clamp01(detection * 0.68 + responseReadiness * 0.22 + comms * 0.1);
}

/**
 * Overall readiness, plus the sentence that makes it useful.
 *
 * "91%" tells an operator nothing they can act on. "Primary weakness: south-west radar
 * coverage degraded — inspect Radar 02" does.
 */
export function buildSystemHealth(
  sensors: Sensor[],
  coverage: CoverageGrid,
  resources: ResponseResource[],
  weather: Weather,
  assets: Asset[],
): SystemHealth {
  const subsystems: SubsystemHealth[] = sensors.map((s) => ({
    id: s.id,
    label: s.name,
    value: s.status === 'offline' ? 0 : s.effectiveness,
    status: s.status === 'offline' ? 'offline' : s.status === 'degraded' ? 'degraded' : 'nominal',
  }));

  const meanCoverage = coverage.cells.reduce((sum, c) => sum + c.value, 0) / Math.max(coverage.cells.length, 1);
  subsystems.push({
    id: 'COVERAGE',
    label: 'Site coverage',
    value: meanCoverage,
    status: meanCoverage > 0.6 ? 'nominal' : meanCoverage > 0.35 ? 'degraded' : 'offline',
  });

  const availableResources = resources.filter((r) => r.status === 'available').length;
  const resourceValue = clamp01(availableResources / 3);
  subsystems.push({
    id: 'RESPONSE',
    label: 'Response resources',
    value: resourceValue,
    status: availableResources >= 2 ? 'nominal' : availableResources >= 1 ? 'degraded' : 'offline',
  });

  const connected = resources.filter((r) => r.communications === 'connected').length;
  const commsValue = resources.length ? connected / resources.length : 1;
  subsystems.push({
    id: 'COMMS',
    label: 'Communications',
    value: commsValue,
    status: commsValue > 0.8 ? 'nominal' : commsValue > 0.5 ? 'degraded' : 'offline',
  });

  // Readiness is dominated by whether the site can be seen. Everything else adjusts it.
  const sensorMean = sensors.length
    ? sensors.reduce((s, x) => s + (x.status === 'offline' ? 0 : x.effectiveness), 0) / sensors.length
    : 0;
  const readiness = clamp01(
    meanCoverage * 0.42 + sensorMean * 0.3 + resourceValue * 0.16 + commsValue * 0.12,
  );

  const { primaryWeakness, recommendedAction } = diagnose(sensors, coverage, resources, weather, assets);

  return { readiness, subsystems, primaryWeakness, recommendedAction };
}

/**
 * Pick the single most important thing wrong, in priority order. Listing every fault is a
 * log; naming the one that matters is a diagnosis.
 */
function diagnose(
  sensors: Sensor[],
  coverage: CoverageGrid,
  resources: ResponseResource[],
  weather: Weather,
  assets: Asset[],
): { primaryWeakness?: string; recommendedAction?: string } {
  const offline = sensors.filter((s) => s.status === 'offline');
  if (offline.length > 0) {
    const s = offline[0]!;
    return {
      primaryWeakness: `${s.name} offline — ${coverage.weakestSector} coverage reduced.`,
      recommendedAction: `Restore ${s.id}. Verify power and network at the ${s.name.split('—')[1]?.trim() ?? 'sensor'} mast.`,
    };
  }

  const gapFraction = coverage.cells.filter((c) => c.quality === 'gap').length / Math.max(coverage.cells.length, 1);
  if (gapFraction > 0.28) {
    return {
      primaryWeakness: `Detection gap across the ${coverage.weakestSector} sector.`,
      recommendedAction: 'Review sensor siting or deploy an additional node to the weak sector.',
    };
  }

  const degraded = sensors.filter((s) => s.status === 'degraded');
  if (degraded.length > 0) {
    const s = degraded[0]!;
    return {
      primaryWeakness: `${s.name} degraded${s.degradationReason ? ` — ${s.degradationReason}` : ''}.`,
      recommendedAction: `Inspect ${s.id}.`,
    };
  }

  if (weather.condition !== 'clear') {
    return {
      primaryWeakness: `${weather.label} reducing electro-optical confidence.`,
      recommendedAction: 'No action available. Radar and RF unaffected; visual identification will be slower.',
    };
  }

  const exposed = assets
    .filter((a) => a.criticality >= 7)
    .sort((a, b) => a.protectionScore - b.protectionScore)[0];
  if (exposed && exposed.protectionScore < 0.7) {
    return {
      primaryWeakness: `${exposed.name} protection at ${Math.round(exposed.protectionScore * 100)}%.`,
      recommendedAction: `Improve sensor coverage over ${exposed.name}.`,
    };
  }

  if (resources.filter((r) => r.status === 'available').length < 2) {
    return {
      primaryWeakness: 'Fewer than two response resources available.',
      recommendedAction: 'Return a resource to availability before the next operational period.',
    };
  }

  return {};
}
