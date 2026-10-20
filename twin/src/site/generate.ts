/**
 * Generating a starting sensor layout and zone set from a boundary and an asset list.
 *
 * Tracing or importing buildings gets a site its physical plant. It gets none of what makes
 * Aetos actually run: nothing is watching it, and nothing is protected. These two functions
 * produce a defensible starting point for both — evenly spaced sensors around the
 * perimeter, a perimeter zone plus a critical volume around anything that earned high
 * criticality — that the operator is expected to open and adjust, not a finished design.
 * Pure and synchronous specifically so the editor can call them from a single button and
 * so their placement logic is checkable without a canvas in the loop.
 */

import type { Ground, ResponseResource, Sensor, Site, Zone } from '@/types';
import { polygonCentroid } from '@/lib/geometry';
import { computeSiteExtent } from './presentation';

const DEG = Math.PI / 180;

/**
 * One sensor at each of `count` evenly spaced points around the boundary, each aimed
 * inward at the centroid with a wide-but-not-omnidirectional sector — a real radar has a
 * blind arc behind its mast, and pretending otherwise would understate exactly the coverage
 * gaps the system exists to surface.
 */
export function generateDefaultSensors(boundary: Ground[], count = 4): Sensor[] {
  if (boundary.length < 3) return [];

  const centroid = polygonCentroid(boundary);
  const perimeterPoints = sampleAroundBoundary(boundary, count);
  const sensors: Sensor[] = [];

  perimeterPoints.forEach((position, i) => {
    const toCentre = Math.atan2(centroid.e - position.e, centroid.n - position.n);
    const range = Math.max(
      Math.hypot(centroid.e - position.e, centroid.n - position.n) * 2.2,
      150,
    );

    sensors.push({
      id: `SEN-${String(i + 1).padStart(2, '0')}`,
      name: `Radar ${String(i + 1).padStart(2, '0')}`,
      type: 'radar',
      position: { e: position.e, n: position.n, u: 0 },
      mastHeight: 12,
      status: 'online',
      health: 1,
      effectiveness: 1,
      coverage: {
        minRange: 20,
        maxRange: range,
        bearing: toCentre < 0 ? toCentre + Math.PI * 2 : toCentre,
        azimuthWidth: 150 * DEG,
        minElevation: -2 * DEG,
        maxElevation: 35 * DEG,
      },
    });
  });

  return sensors;
}

/** One default interceptor and one ground patrol — an inventory, not a claim of readiness. */
export function generateDefaultResources(boundary: Ground[]): ResponseResource[] {
  if (boundary.length < 3) return [];
  const centroid = polygonCentroid(boundary);
  return [
    {
      id: 'RES-01',
      name: 'Interceptor 01',
      kind: 'Interceptor UAS',
      position: { e: centroid.e, n: centroid.n, u: 0 },
      status: 'available',
      readiness: 0.9,
      batteryPercent: 90,
      communications: 'connected',
    },
    {
      id: 'RES-02',
      name: 'Patrol 01',
      kind: 'Ground patrol',
      position: { e: centroid.e, n: centroid.n, u: 0 },
      status: 'available',
      readiness: 0.85,
      communications: 'connected',
    },
  ];
}

/**
 * A perimeter zone from the boundary itself, plus a restricted and/or critical volume
 * around any asset whose criticality earns one. Thresholds match the demo site's own
 * convention (ADR-equivalent: `site/demo-site.ts`) — 6+ restricted, 8+ critical — so a
 * generated site and the hand-authored one read the same way once loaded.
 */
export function generateDefaultZones(site: Pick<Site, 'boundary' | 'assets'>): {
  zones: Zone[];
  /** assetId -> zoneId, so the caller can also set `protectionZoneId` on each asset. */
  assignments: Record<string, string>;
} {
  const zones: Zone[] = [];
  const assignments: Record<string, string> = {};

  if (site.boundary.length >= 3) {
    const extent = computeSiteExtent({ boundary: site.boundary, assets: site.assets });
    zones.push({
      id: 'ZONE-PERIM',
      name: 'Site Perimeter',
      zoneClass: 'perimeter',
      polygon: site.boundary,
      floor: 0,
      ceiling: Math.max(60, extent * 0.15),
      emphasis: 0,
      intruderTrackIds: [],
      predictedIntruderTrackIds: [],
    });
  }

  let index = 0;
  for (const asset of site.assets) {
    if (asset.criticality < 6) continue;
    index += 1;

    const isCritical = asset.criticality >= 8;
    const margin = Math.max(asset.footprint.width, asset.footprint.depth) * (isCritical ? 0.6 : 1.0) + 15;
    const zoneId = `ZONE-${isCritical ? 'CRIT' : 'REST'}-${String(index).padStart(2, '0')}`;

    zones.push({
      id: zoneId,
      name: `${isCritical ? 'Critical' : 'Restricted'} — ${asset.name}`,
      zoneClass: isCritical ? 'critical' : 'restricted',
      polygon: squareAround(asset.position, margin),
      floor: 0,
      ceiling: isCritical ? 130 : 90,
      protectsAssetId: asset.id,
      emphasis: 0,
      intruderTrackIds: [],
      predictedIntruderTrackIds: [],
    });
    assignments[asset.id] = zoneId;
  }

  return { zones, assignments };
}

// ---------------------------------------------------------------------------

function sampleAroundBoundary(boundary: Ground[], count: number): Ground[] {
  const perimeter: { a: Ground; b: Ground; length: number }[] = [];
  let total = 0;
  for (let i = 0, j = boundary.length - 1; i < boundary.length; j = i++) {
    const a = boundary[j]!;
    const b = boundary[i]!;
    const length = Math.hypot(b.e - a.e, b.n - a.n);
    perimeter.push({ a, b, length });
    total += length;
  }
  if (total === 0) return [];

  const points: Ground[] = [];
  for (let k = 0; k < count; k++) {
    let target = (k / count) * total;
    for (const seg of perimeter) {
      if (target <= seg.length) {
        const t = seg.length === 0 ? 0 : target / seg.length;
        points.push({ e: seg.a.e + (seg.b.e - seg.a.e) * t, n: seg.a.n + (seg.b.n - seg.a.n) * t });
        break;
      }
      target -= seg.length;
    }
  }
  return points;
}

function squareAround(centre: { e: number; n: number; u: number }, halfSpan: number): Ground[] {
  return [
    { e: centre.e - halfSpan, n: centre.n - halfSpan },
    { e: centre.e + halfSpan, n: centre.n - halfSpan },
    { e: centre.e + halfSpan, n: centre.n + halfSpan },
    { e: centre.e - halfSpan, n: centre.n + halfSpan },
  ];
}
