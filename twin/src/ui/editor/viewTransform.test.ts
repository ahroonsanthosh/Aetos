import { describe, expect, it } from 'vitest';
import {
  clampScale,
  enuToScreen,
  fitViewTo,
  MAX_METRES_PER_PIXEL,
  MIN_METRES_PER_PIXEL,
  panByPixels,
  scaleBarFor,
  screenToEnu,
  zoomAt,
  zoomByFactor,
  type ViewTransform,
} from './viewTransform';

const W = 800;
const H = 600;

const view: ViewTransform = { centreE: 100, centreN: -50, metresPerPixel: 2 };

describe('enuToScreen / screenToEnu', () => {
  it('puts the view centre at the canvas centre', () => {
    const screen = enuToScreen({ e: view.centreE, n: view.centreN }, view, W, H);
    expect(screen.x).toBeCloseTo(W / 2, 9);
    expect(screen.y).toBeCloseTo(H / 2, 9);
  });

  it('maps north upward on screen', () => {
    const north = enuToScreen({ e: view.centreE, n: view.centreN + 100 }, view, W, H);
    expect(north.y).toBeLessThan(H / 2);
  });

  it('round-trips an arbitrary point', () => {
    const original = { x: 613, y: 122 };
    const back = enuToScreen(screenToEnu(original, view, W, H), view, W, H);
    expect(back.x).toBeCloseTo(original.x, 9);
    expect(back.y).toBeCloseTo(original.y, 9);
  });
});

describe('zoomAt', () => {
  it('holds the ground point under the anchor exactly still', () => {
    const anchor = { x: 640, y: 180 };
    const before = screenToEnu(anchor, view, W, H);

    for (const factor of [0.5, 0.8, 1.25, 2, 4]) {
      const after = zoomAt(view, factor, anchor, W, H);
      const groundAfter = screenToEnu(anchor, after, W, H);
      expect(groundAfter.e).toBeCloseTo(before.e, 6);
      expect(groundAfter.n).toBeCloseTo(before.n, 6);
    }
  });

  it('holds the point still even at the anchor being a canvas corner', () => {
    const anchor = { x: 0, y: 0 };
    const before = screenToEnu(anchor, view, W, H);
    const after = zoomAt(view, 3, anchor, W, H);
    const groundAfter = screenToEnu(anchor, after, W, H);
    expect(groundAfter.e).toBeCloseTo(before.e, 6);
    expect(groundAfter.n).toBeCloseTo(before.n, 6);
  });

  it('zooming in then out by the reciprocal returns to the original view', () => {
    const anchor = { x: 300, y: 400 };
    const round = zoomAt(zoomAt(view, 0.5, anchor, W, H), 2, anchor, W, H);
    expect(round.metresPerPixel).toBeCloseTo(view.metresPerPixel, 9);
    expect(round.centreE).toBeCloseTo(view.centreE, 6);
    expect(round.centreN).toBeCloseTo(view.centreN, 6);
  });

  it('respects the scale limits', () => {
    expect(zoomAt(view, 1e-9, { x: 0, y: 0 }, W, H).metresPerPixel).toBeGreaterThanOrEqual(
      MIN_METRES_PER_PIXEL,
    );
    expect(zoomAt(view, 1e9, { x: 0, y: 0 }, W, H).metresPerPixel).toBeLessThanOrEqual(
      MAX_METRES_PER_PIXEL,
    );
  });
});

describe('zoomByFactor', () => {
  it('changes scale without moving the centre', () => {
    const zoomed = zoomByFactor(view, 0.5);
    expect(zoomed.metresPerPixel).toBeCloseTo(1, 9);
    expect(zoomed.centreE).toBe(view.centreE);
    expect(zoomed.centreN).toBe(view.centreN);
  });

  it('clamps at both ends', () => {
    expect(clampScale(1e9)).toBe(MAX_METRES_PER_PIXEL);
    expect(clampScale(0)).toBe(MIN_METRES_PER_PIXEL);
  });
});

describe('panByPixels', () => {
  it('moves the world with the drag, not against it', () => {
    // Dragging right should reveal what was to the west, so the centre moves west.
    const panned = panByPixels(view, 50, 0);
    expect(panned.centreE).toBeLessThan(view.centreE);

    // Dragging down should reveal what was to the north, so the centre moves north.
    const down = panByPixels(view, 0, 50);
    expect(down.centreN).toBeGreaterThan(view.centreN);
  });

  it('keeps the ground point under the pointer fixed across the drag', () => {
    const start = { x: 400, y: 300 };
    const groundAtStart = screenToEnu(start, view, W, H);

    const dx = 120;
    const dy = -80;
    const panned = panByPixels(view, dx, dy);
    const groundAtEnd = screenToEnu({ x: start.x + dx, y: start.y + dy }, panned, W, H);

    expect(groundAtEnd.e).toBeCloseTo(groundAtStart.e, 6);
    expect(groundAtEnd.n).toBeCloseTo(groundAtStart.n, 6);
  });

  it('leaves the scale untouched', () => {
    expect(panByPixels(view, 33, -21).metresPerPixel).toBe(view.metresPerPixel);
  });
});

describe('scaleBarFor', () => {
  it('picks a round distance from the ladder', () => {
    const ladder = [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10_000];
    for (const mpp of [0.1, 0.5, 2, 10, 60]) {
      expect(ladder).toContain(scaleBarFor({ ...view, metresPerPixel: mpp }, W).metres);
    }
  });

  it('reports a pixel length consistent with the chosen distance', () => {
    const bar = scaleBarFor(view, W);
    expect(bar.pixels).toBeCloseTo(bar.metres / view.metresPerPixel, 9);
  });

  it('keeps the bar a usable fraction of the canvas at any zoom', () => {
    for (const mpp of [0.05, 1, 25, 200]) {
      const bar = scaleBarFor({ ...view, metresPerPixel: mpp }, W);
      expect(bar.pixels).toBeGreaterThan(W * 0.04);
      expect(bar.pixels).toBeLessThan(W * 0.6);
    }
  });
});

describe('fitViewTo', () => {
  it('centres on the bounding box of the points', () => {
    const fitted = fitViewTo(
      [
        { e: -100, n: -50 },
        { e: 300, n: 150 },
      ],
      W,
      H,
    );
    expect(fitted.centreE).toBeCloseTo(100, 6);
    expect(fitted.centreN).toBeCloseTo(50, 6);
  });

  it('frames every point inside the canvas', () => {
    const points = [
      { e: -400, n: -220 },
      { e: 380, n: 260 },
      { e: 10, n: -100 },
    ];
    const fitted = fitViewTo(points, W, H);
    for (const p of points) {
      const screen = enuToScreen(p, fitted, W, H);
      expect(screen.x).toBeGreaterThanOrEqual(0);
      expect(screen.x).toBeLessThanOrEqual(W);
      expect(screen.y).toBeGreaterThanOrEqual(0);
      expect(screen.y).toBeLessThanOrEqual(H);
    }
  });

  it('returns a usable view for an empty point list', () => {
    const fitted = fitViewTo([], W, H);
    expect(fitted.metresPerPixel).toBeGreaterThan(0);
  });
});
