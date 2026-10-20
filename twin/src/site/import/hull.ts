/**
 * Convex hull and outward buffer — used once, to turn a cloud of imported building corners
 * into a plausible starting site boundary. Not a substitute for the operator drawing the
 * real fence line; a hull can't know where the perimeter actually runs relative to the
 * buildings inside it. It exists so the editor never opens onto an empty boundary after an
 * OSM import with buildings and nothing enclosing them.
 */

import type { Ground } from '@/types';

/** Monotone chain. O(n log n), no dependency, and the input here never exceeds a few hundred points. */
export function convexHull(points: Ground[]): Ground[] {
  if (points.length < 3) return points.slice();

  const sorted = [...points].sort((a, b) => a.e - b.e || a.n - b.n);
  const cross = (o: Ground, a: Ground, b: Ground) =>
    (a.e - o.e) * (b.n - o.n) - (a.n - o.n) * (b.e - o.e);

  const lower: Ground[] = [];
  for (const p of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2]!, lower[lower.length - 1]!, p) <= 0) {
      lower.pop();
    }
    lower.push(p);
  }

  const upper: Ground[] = [];
  for (let i = sorted.length - 1; i >= 0; i--) {
    const p = sorted[i]!;
    while (upper.length >= 2 && cross(upper[upper.length - 2]!, upper[upper.length - 1]!, p) <= 0) {
      upper.pop();
    }
    upper.push(p);
  }

  lower.pop();
  upper.pop();
  return lower.concat(upper);
}

/**
 * Push every vertex of a convex polygon outward from its centroid by `margin` metres.
 *
 * A proper polygon offset follows each edge's own normal; this is the centroid-radial
 * approximation, which is exact for a regular polygon and close enough for an irregular one
 * used only as an editable starting guess. The operator is expected to adjust it.
 */
export function bufferOutward(polygon: Ground[], margin: number): Ground[] {
  if (polygon.length === 0) return polygon;

  const centroid = polygon.reduce(
    (acc, p) => ({ e: acc.e + p.e / polygon.length, n: acc.n + p.n / polygon.length }),
    { e: 0, n: 0 },
  );

  return polygon.map((p) => {
    const dx = p.e - centroid.e;
    const dy = p.n - centroid.n;
    const length = Math.hypot(dx, dy) || 1;
    const scale = (length + margin) / length;
    return { e: centroid.e + dx * scale, n: centroid.n + dy * scale };
  });
}

/** Convenience: hull the input, then buffer it. What the OSM importer calls directly. */
export function suggestBoundary(points: Ground[], margin: number): Ground[] {
  return bufferOutward(convexHull(points), margin);
}
