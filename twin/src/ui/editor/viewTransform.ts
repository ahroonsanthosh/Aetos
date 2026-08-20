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
