import { describe, expect, it } from 'vitest';
import { bufferOutward, convexHull, suggestBoundary } from './hull';
import { pointInPolygon, polygonCentroid } from '@/lib/geometry';

describe('convexHull', () => {
  it('drops interior points', () => {
    const points = [
      { e: 0, n: 0 },
      { e: 10, n: 0 },
      { e: 10, n: 10 },
      { e: 0, n: 10 },
      { e: 5, n: 5 }, // centre — not on the hull
    ];
    const hull = convexHull(points);
    expect(hull).toHaveLength(4);
    expect(hull.some((p) => p.e === 5 && p.n === 5)).toBe(false);
  });

  it('encloses every input point', () => {
    const points = Array.from({ length: 30 }, (_, i) => ({
      e: Math.sin(i) * 40 + 10,
      n: Math.cos(i * 1.7) * 25 - 5,
    }));
    const hull = convexHull(points);
    for (const p of points) {
      // A point on the hull boundary itself may not satisfy strict interior containment;
      // check it isn't clearly outside by more than a rounding margin.
      const inside = pointInPolygon(p, hull);
      const onHull = hull.some((h) => Math.abs(h.e - p.e) < 1e-9 && Math.abs(h.n - p.n) < 1e-9);
      expect(inside || onHull).toBe(true);
    }
  });

  it('handles fewer than three points without throwing', () => {
    expect(convexHull([])).toEqual([]);
    expect(convexHull([{ e: 1, n: 1 }])).toEqual([{ e: 1, n: 1 }]);
  });
});

describe('bufferOutward', () => {
  it('pushes every vertex further from the centroid', () => {
    const square = [
      { e: -10, n: -10 },
      { e: 10, n: -10 },
      { e: 10, n: 10 },
      { e: -10, n: 10 },
    ];
    const buffered = bufferOutward(square, 5);
    const centroid = polygonCentroid(square);

    for (let i = 0; i < square.length; i++) {
      const before = Math.hypot(square[i]!.e - centroid.e, square[i]!.n - centroid.n);
      const after = Math.hypot(buffered[i]!.e - centroid.e, buffered[i]!.n - centroid.n);
      expect(after).toBeGreaterThan(before);
    }
  });

  it('encloses the original polygon', () => {
    const square = [
      { e: -10, n: -10 },
      { e: 10, n: -10 },
      { e: 10, n: 10 },
      { e: -10, n: 10 },
    ];
    const buffered = bufferOutward(square, 8);
    for (const corner of square) {
      expect(pointInPolygon(corner, buffered)).toBe(true);
    }
  });
});

describe('suggestBoundary', () => {
  it('hulls then buffers, enclosing every input point', () => {
    const points = [
      { e: 0, n: 0 },
      { e: 100, n: 0 },
      { e: 100, n: 60 },
      { e: 0, n: 60 },
      { e: 50, n: 30 },
    ];
    const boundary = suggestBoundary(points, 20);
    for (const p of points) {
      expect(pointInPolygon(p, boundary)).toBe(true);
    }
  });
});
