/**
 * Calibration math tests.
 *
 * The rotation in `pixelToGround` is exactly the kind of thing that is easy to get subtly
 * wrong by eye — a flipped sign produces a site that traces perfectly at one bearing and
 * mirrors itself at another. Every case here is checked against a hand-derived expectation,
 * not just round-tripped against its own inverse, so a consistent-but-wrong transform cannot
 * pass by agreeing with itself.
 */

import { describe, expect, it } from 'vitest';
import type { Calibration } from './calibration';
import { groundToPixel, metresPerPixel, pixelToGround, tracePolygonToGround } from './calibration';

const HALF_PI = Math.PI / 2;

function calibration(overrides: Partial<Calibration> = {}): Calibration {
  return {
    pointA: { x: 0, y: 0 },
    pointB: { x: 100, y: 0 },
    realDistanceMetres: 50,
    originPixel: { x: 0, y: 0 },
    northBearing: 0,
    ...overrides,
  };
}

describe('metresPerPixel', () => {
  it('divides the real distance by the pixel distance', () => {
    const c = calibration({ pointA: { x: 0, y: 0 }, pointB: { x: 200, y: 0 }, realDistanceMetres: 50 });
    expect(metresPerPixel(c)).toBeCloseTo(0.25, 10);
  });

  it('is unaffected by the calibration points not being axis-aligned', () => {
    // 3-4-5 triangle: 300 px apart, 30 m real → 0.1 m/px.
    const c = calibration({ pointA: { x: 0, y: 0 }, pointB: { x: 240, y: 180 }, realDistanceMetres: 30 });
    expect(metresPerPixel(c)).toBeCloseTo(0.1, 10);
  });
});

describe('pixelToGround — north-up (bearing 0)', () => {
  const c = calibration({
    pointA: { x: 0, y: 0 },
    pointB: { x: 100, y: 0 },
    realDistanceMetres: 100, // 1 m/px
    originPixel: { x: 500, y: 500 },
    northBearing: 0,
  });

  it('maps rightward in the image to east', () => {
    const g = pixelToGround({ x: 600, y: 500 }, c);
    expect(g.e).toBeCloseTo(100, 6);
    expect(g.n).toBeCloseTo(0, 6);
  });

  it('maps upward in the image (smaller y) to north', () => {
    const g = pixelToGround({ x: 500, y: 400 }, c);
    expect(g.e).toBeCloseTo(0, 6);
    expect(g.n).toBeCloseTo(100, 6);
  });

  it('maps downward in the image to south', () => {
    const g = pixelToGround({ x: 500, y: 600 }, c);
    expect(g.n).toBeCloseTo(-100, 6);
  });

  it('leaves the origin pixel at the ENU origin', () => {
    const g = pixelToGround({ x: 500, y: 500 }, c);
    expect(g.e).toBeCloseTo(0, 6);
    expect(g.n).toBeCloseTo(0, 6);
  });
});

describe('pixelToGround — rotated image (90° bearing: image "up" points east)', () => {
  const c = calibration({
    pointA: { x: 0, y: 0 },
    pointB: { x: 100, y: 0 },
    realDistanceMetres: 100, // 1 m/px
    originPixel: { x: 0, y: 0 },
    northBearing: HALF_PI,
  });

  it('maps upward in the image to east, not north', () => {
    const g = pixelToGround({ x: 0, y: -100 }, c);
    expect(g.e).toBeCloseTo(100, 6);
    expect(g.n).toBeCloseTo(0, 6);
  });

  it('maps rightward in the image to south', () => {
    // "Right" is 90° clockwise from "up"; up = east, so right = south.
    const g = pixelToGround({ x: 100, y: 0 }, c);
    expect(g.e).toBeCloseTo(0, 6);
    expect(g.n).toBeCloseTo(-100, 6);
  });
});

describe('pixelToGround / groundToPixel round trip', () => {
  const cases: Calibration[] = [
    calibration({ northBearing: 0 }),
    calibration({ northBearing: HALF_PI }),
    calibration({ northBearing: Math.PI }),
    calibration({ northBearing: -1.9 }),
    calibration({ northBearing: 0.7, originPixel: { x: 340, y: -120 } }),
  ];

  for (const c of cases) {
    it(`recovers the original pixel at bearing ${c.northBearing.toFixed(2)}`, () => {
      const original = { x: 723, y: 118 };
      const ground = pixelToGround(original, c);
      const back = groundToPixel(ground, c);
      expect(back.x).toBeCloseTo(original.x, 6);
      expect(back.y).toBeCloseTo(original.y, 6);
    });
  }
});

describe('tracePolygonToGround', () => {
  it('converts every vertex of a traced polygon', () => {
    const c = calibration({
      pointA: { x: 0, y: 0 },
      pointB: { x: 100, y: 0 },
      realDistanceMetres: 100,
      originPixel: { x: 0, y: 0 },
    });

    const polygon = tracePolygonToGround(
      [
        { x: 0, y: 0 },
        { x: 100, y: 0 },
        { x: 100, y: -100 },
        { x: 0, y: -100 },
      ],
      c,
    );

    expect(polygon).toHaveLength(4);
    // toBeCloseTo rather than toEqual: the rotation legitimately produces -0 for an exact
    // zero component, which is numerically identical to 0 but fails strict deep equality.
    expect(polygon[0]!.e).toBeCloseTo(0, 9);
    expect(polygon[0]!.n).toBeCloseTo(0, 9);
    expect(polygon[1]!.e).toBeCloseTo(100, 6);
    expect(polygon[2]!.n).toBeCloseTo(100, 6);
  });
});
