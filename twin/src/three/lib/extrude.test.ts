/**
 * Pure ENU geometry helpers used for facade detail placement.
 *
 * The one thing worth pinning here is winding-independence: an operator's trace clicks are
 * never guaranteed to go clockwise or counter-clockwise, so `sampleFacade` has to resolve
 * "outward" from the polygon's own signed area rather than assume an order — a wrong
 * assumption there would point every window into the building instead of away from it.
 */

import { describe, expect, it } from 'vitest';
import type { Ground } from '@/types';
import { nearestCompassDirection, polygonEdges, polygonSignedArea, sampleFacade } from './extrude';

const ccwSquare: Ground[] = [
  { e: 0, n: 0 },
  { e: 10, n: 0 },
  { e: 10, n: 10 },
  { e: 0, n: 10 },
];

const cwSquare = [...ccwSquare].reverse();

describe('polygonSignedArea', () => {
  it('is positive for a counter-clockwise polygon', () => {
    expect(polygonSignedArea(ccwSquare)).toBeGreaterThan(0);
  });

  it('is negative for the same polygon reversed', () => {
    expect(polygonSignedArea(cwSquare)).toBeLessThan(0);
  });

  it('has the magnitude of the polygon area', () => {
    expect(Math.abs(polygonSignedArea(ccwSquare))).toBeCloseTo(100, 6);
  });
});

describe('sampleFacade', () => {
  it('places points along every edge at roughly the requested spacing', () => {
    const points = sampleFacade(ccwSquare, 5);
    // 4 edges of 10m at 5m spacing: 2 points per edge, 8 total.
    expect(points.length).toBe(8);
  });

  it('points the outward normal away from the polygon centroid, not into it', () => {
    for (const { point, normal } of sampleFacade(ccwSquare, 5)) {
      const stepOut = { e: point.e + normal.e * 0.5, n: point.n + normal.n * 0.5 };
      const stepIn = { e: point.e - normal.e * 0.5, n: point.n - normal.n * 0.5 };
      const centroid = { e: 5, n: 5 };
      const distOut = Math.hypot(stepOut.e - centroid.e, stepOut.n - centroid.n);
      const distIn = Math.hypot(stepIn.e - centroid.e, stepIn.n - centroid.n);
      expect(distOut).toBeGreaterThan(distIn);
    }
  });

  it('agrees on outward direction regardless of winding order', () => {
    const ccwNormals = sampleFacade(ccwSquare, 5).map((p) => p.normal);
    const cwNormals = sampleFacade(cwSquare, 5).map((p) => p.normal);

    // Same set of edges, so the same set of outward normals should appear from either
    // winding — a mismatch here means a clockwise trace would face its details inward.
    const key = (n: { e: number; n: number }) => `${n.e.toFixed(3)},${n.n.toFixed(3)}`;
    const ccwSet = new Set(ccwNormals.map(key));
    const cwSet = new Set(cwNormals.map(key));
    expect(cwSet).toEqual(ccwSet);
  });

  it('returns a unit normal', () => {
    for (const { normal } of sampleFacade(ccwSquare, 3)) {
      expect(Math.hypot(normal.e, normal.n)).toBeCloseTo(1, 6);
    }
  });
});

describe('polygonEdges', () => {
  it('returns one edge per side, each with its real length', () => {
    const edges = polygonEdges(ccwSquare);
    expect(edges).toHaveLength(4);
    for (const edge of edges) expect(edge.length).toBeCloseTo(10, 6);
  });

  it('agrees with sampleFacade on which way is outward', () => {
    const key = (n: { e: number; n: number }) => `${n.e.toFixed(3)},${n.n.toFixed(3)}`;
    const edgeNormals = new Set(polygonEdges(ccwSquare).map((e) => key(e.normal)));
    const sampledNormals = new Set(sampleFacade(ccwSquare, 3).map((p) => key(p.normal)));
    expect(sampledNormals).toEqual(edgeNormals);
  });
});

describe('nearestCompassDirection', () => {
  it('classifies each side of a due-north-aligned square correctly', () => {
    const byDirection = new Map(
      polygonEdges(ccwSquare).map((edge) => [nearestCompassDirection(edge.normal), edge]),
    );
    // South (bottom, n=0), east (right, e=10), north (top, n=10), west (left, e=0).
    expect(byDirection.get('south')!.a).toEqual({ e: 0, n: 0 });
    expect(byDirection.get('east')!.a).toEqual({ e: 10, n: 0 });
    expect(byDirection.get('north')!.a).toEqual({ e: 10, n: 10 });
    expect(byDirection.get('west')!.a).toEqual({ e: 0, n: 10 });
  });

  it('picks the nearest side for an off-axis wall rather than refusing to classify it', () => {
    // A wall facing 10° east of due north should still read as "north".
    const angle = (10 * Math.PI) / 180;
    expect(nearestCompassDirection({ e: Math.sin(angle), n: Math.cos(angle) })).toBe('north');
  });

  it('agrees regardless of the polygon\'s winding order', () => {
    const cwSquare = [...ccwSquare].reverse();
    const ccwDirections = polygonEdges(ccwSquare)
      .map((e) => nearestCompassDirection(e.normal))
      .sort();
    const cwDirections = polygonEdges(cwSquare)
      .map((e) => nearestCompassDirection(e.normal))
      .sort();
    expect(cwDirections).toEqual(ccwDirections);
  });
});
