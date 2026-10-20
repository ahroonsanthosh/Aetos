/**
 * Planar and 3D helpers shared by the simulation and the renderer.
 *
 * Everything here operates in the ENU domain frame. Nothing in this file knows about
 * three.js — the axis conversion lives in `three/frame.ts`.
 */

import type { Enu, Ground } from '@/types';

export const TAU = Math.PI * 2;

export function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

export function clamp01(v: number): number {
  return clamp(v, 0, 1);
}

export function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

export function lerpEnu(a: Enu, b: Enu, t: number): Enu {
  return { e: lerp(a.e, b.e, t), n: lerp(a.n, b.n, t), u: lerp(a.u, b.u, t) };
}

/** Smoothstep easing, used wherever a value is exposed to the eye rather than to maths. */
export function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = clamp01((x - edge0) / (edge1 - edge0));
  return t * t * (3 - 2 * t);
}

/**
 * Frame-rate independent exponential approach. `smoothing` is the fraction of the gap
 * remaining after one second, so smaller values converge faster.
 */
export function damp(current: number, target: number, smoothing: number, dt: number): number {
  return lerp(target, current, Math.pow(smoothing, dt));
}

export function distanceGround(a: Ground, b: Ground): number {
  return Math.hypot(b.e - a.e, b.n - a.n);
}

export function distance3(a: Enu, b: Enu): number {
  return Math.hypot(b.e - a.e, b.n - a.n, b.u - a.u);
}

/** Horizontal distance only. Range to a site is a ground range, not a slant range. */
export function horizontalDistance(a: Enu | Ground, b: Enu | Ground): number {
  return Math.hypot(b.e - a.e, b.n - a.n);
}

/** Bearing from `from` to `to`, radians clockwise from north, in [0, 2π). */
export function bearing(from: Enu | Ground, to: Enu | Ground): number {
  const angle = Math.atan2(to.e - from.e, to.n - from.n);
  return angle < 0 ? angle + TAU : angle;
}

/** Smallest signed difference between two bearings, in (-π, π]. */
export function angleDelta(a: number, b: number): number {
  let d = (b - a) % TAU;
  if (d > Math.PI) d -= TAU;
  if (d <= -Math.PI) d += TAU;
  return d;
}

// ---------------------------------------------------------------------------
// Polygons
// ---------------------------------------------------------------------------

/** Ray-casting point-in-polygon. Polygon is treated as closed. */
export function pointInPolygon(p: Ground, polygon: Ground[]): boolean {
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[i]!;
    const b = polygon[j]!;
    const intersects =
      a.n > p.n !== b.n > p.n && p.e < ((b.e - a.e) * (p.n - a.n)) / (b.n - a.n) + a.e;
    if (intersects) inside = !inside;
  }
  return inside;
}

/** Centroid of a polygon's vertices. Adequate for camera framing and label anchoring. */
export function polygonCentroid(polygon: Ground[]): Ground {
  let e = 0;
  let n = 0;
  for (const p of polygon) {
    e += p.e;
    n += p.n;
  }
  return { e: e / polygon.length, n: n / polygon.length };
}

/** Shortest distance from a point to a polygon's boundary. */
export function distanceToPolygonEdge(p: Ground, polygon: Ground[]): number {
  let best = Infinity;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    best = Math.min(best, distanceToSegment(p, polygon[j]!, polygon[i]!));
  }
  return best;
}

export function distanceToSegment(p: Ground, a: Ground, b: Ground): number {
  const de = b.e - a.e;
  const dn = b.n - a.n;
  const lenSq = de * de + dn * dn;
  if (lenSq === 0) return distanceGround(p, a);
  const t = clamp01(((p.e - a.e) * de + (p.n - a.n) * dn) / lenSq);
  return Math.hypot(p.e - (a.e + t * de), p.n - (a.n + t * dn));
}

/**
 * Signed distance to a polygon: negative inside, positive outside. Used for zone emphasis,
 * where "how far outside" matters as much as "in or out".
 */
export function signedDistanceToPolygon(p: Ground, polygon: Ground[]): number {
  const d = distanceToPolygonEdge(p, polygon);
  return pointInPolygon(p, polygon) ? -d : d;
}

/** Axis-aligned bounds of a polygon. */
export function polygonBounds(polygon: Ground[]): {
  minE: number;
  maxE: number;
  minN: number;
  maxN: number;
} {
  let minE = Infinity;
  let maxE = -Infinity;
  let minN = Infinity;
  let maxN = -Infinity;
  for (const p of polygon) {
    if (p.e < minE) minE = p.e;
    if (p.e > maxE) maxE = p.e;
    if (p.n < minN) minN = p.n;
    if (p.n > maxN) maxN = p.n;
  }
  return { minE, maxE, minN, maxN };
}

// ---------------------------------------------------------------------------
// Volumes
// ---------------------------------------------------------------------------

/** Is a 3D point inside an extruded polygon volume? */
export function pointInVolume(
  p: Enu,
  polygon: Ground[],
  floor: number,
  ceiling: number,
): boolean {
  if (p.u < floor || p.u > ceiling) return false;
  return pointInPolygon({ e: p.e, n: p.n }, polygon);
}

/**
 * First point along a polyline that enters a volume, with the elapsed time to reach it.
 *
 * Returns undefined if the path never enters. `times` are the timestamps of each point, so
 * the caller gets an ETA rather than an index.
 */
export function firstEntryIntoVolume(
  path: { position: Enu; t: number }[],
  polygon: Ground[],
  floor: number,
  ceiling: number,
): { t: number; position: Enu } | undefined {
  for (const point of path) {
    if (pointInVolume(point.position, polygon, floor, ceiling)) {
      return { t: point.t, position: point.position };
    }
  }
  return undefined;
}
