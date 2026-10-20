/**
 * Screen pixels ↔ ENU metres, for the editor's 2D top-down canvas.
 *
 * Deliberately a second, simpler transform from `site/import/calibration.ts`. Calibration
 * answers "where on the ground does this pixel of the *uploaded image* correspond to",
 * fixed once when the operator calibrates the image and never touched again. This answers
 * "where on the *canvas* am I looking right now" — pan and zoom, changing on every scroll
 * and drag, always north-up. Conflating the two would mean panning the view silently
 * reinterpreting where the image's own pixels point, which is not what either operation
 * means.
 */

import type { Ground } from '@/types';

export interface ViewTransform {
  /** ENU point rendered at the canvas centre. */
  centreE: number;
  centreN: number;
  metresPerPixel: number;
}

export function enuToScreen(
  point: Ground,
  view: ViewTransform,
  canvasWidth: number,
  canvasHeight: number,
): { x: number; y: number } {
  return {
    x: canvasWidth / 2 + (point.e - view.centreE) / view.metresPerPixel,
    // Screen y grows downward; north (+n) should move up the screen, hence the minus.
    y: canvasHeight / 2 - (point.n - view.centreN) / view.metresPerPixel,
  };
}

export function screenToEnu(
  screen: { x: number; y: number },
  view: ViewTransform,
  canvasWidth: number,
  canvasHeight: number,
): Ground {
  return {
    e: view.centreE + (screen.x - canvasWidth / 2) * view.metresPerPixel,
    n: view.centreN - (screen.y - canvasHeight / 2) * view.metresPerPixel,
  };
}

/** A view that frames a boundary (or any point cloud) with margin, for "fit to site". */
export function fitViewTo(points: Ground[], canvasWidth: number, canvasHeight: number): ViewTransform {
  if (points.length === 0) return { centreE: 0, centreN: 0, metresPerPixel: 1 };

  let minE = Infinity;
  let maxE = -Infinity;
  let minN = Infinity;
  let maxN = -Infinity;
  for (const p of points) {
    minE = Math.min(minE, p.e);
    maxE = Math.max(maxE, p.e);
    minN = Math.min(minN, p.n);
    maxN = Math.max(maxN, p.n);
  }

  const spanE = Math.max(maxE - minE, 10);
  const spanN = Math.max(maxN - minN, 10);
  const margin = 1.25;

  const metresPerPixel = Math.max(
    (spanE * margin) / canvasWidth,
    (spanN * margin) / canvasHeight,
  );

  return {
    centreE: (minE + maxE) / 2,
    centreN: (minN + maxN) / 2,
    metresPerPixel: metresPerPixel || 1,
  };
}

/** Zoom limits, metres per pixel. Below the floor the basemap has no more detail to give;
 *  above the ceiling a site is a dot. */
export const MIN_METRES_PER_PIXEL = 0.05;
export const MAX_METRES_PER_PIXEL = 200;

export function clampScale(metresPerPixel: number): number {
  return Math.min(MAX_METRES_PER_PIXEL, Math.max(MIN_METRES_PER_PIXEL, metresPerPixel));
}

/**
 * Zoom while holding one screen point still.
 *
 * Anchoring on the cursor rather than the canvas centre is what makes wheel zoom feel like
 * a map instead of a slideshow: you point at the thing you want a closer look at and it
 * stays under the pointer, rather than sliding away as the centre magnifies.
 *
 * Derived by requiring `screenToEnu(anchor, after) === screenToEnu(anchor, before)` and
 * solving for the new centre.
 */
export function zoomAt(
  view: ViewTransform,
  factor: number,
  anchor: { x: number; y: number },
  canvasWidth: number,
  canvasHeight: number,
): ViewTransform {
  const ground = screenToEnu(anchor, view, canvasWidth, canvasHeight);
  const metresPerPixel = clampScale(view.metresPerPixel * factor);

  return {
    metresPerPixel,
    centreE: ground.e - (anchor.x - canvasWidth / 2) * metresPerPixel,
    centreN: ground.n + (anchor.y - canvasHeight / 2) * metresPerPixel,
  };
}

/** Zoom about the canvas centre — what the +/− buttons use, where there is no cursor. */
export function zoomByFactor(view: ViewTransform, factor: number): ViewTransform {
  return { ...view, metresPerPixel: clampScale(view.metresPerPixel * factor) };
}

/** Pan by a screen-pixel delta, as a drag produces. */
export function panByPixels(view: ViewTransform, dx: number, dy: number): ViewTransform {
  return {
    ...view,
    centreE: view.centreE - dx * view.metresPerPixel,
    centreN: view.centreN + dy * view.metresPerPixel,
  };
}

/**
 * A round distance that occupies a sensible fraction of the canvas, plus its pixel length —
 * everything a scale bar needs. Without one, "how big is this" is guesswork at every zoom.
 */
export function scaleBarFor(view: ViewTransform, canvasWidth: number): { metres: number; pixels: number } {
  const target = canvasWidth * 0.18 * view.metresPerPixel;
  const ladder = [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10_000];
  const metres = ladder.reduce((best, c) => (Math.abs(c - target) < Math.abs(best - target) ? c : best));
  return { metres, pixels: metres / view.metresPerPixel };
}
