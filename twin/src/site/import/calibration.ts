/**
 * Turning a traced image into ground coordinates.
 *
 * An uploaded satellite screenshot or site plan has no scale and no orientation until the
 * operator gives it both. Calibration is two clicks and one number: click two points whose
 * real-world distance you know (two fence corners, the ends of a building), type that
 * distance in metres. From that alone the image's pixels-per-metre scale is fixed.
 *
 * Orientation is a third, optional input — the compass bearing that the image's "up"
 * direction points toward — because a screenshot is almost never north-up. Left unset, "up"
 * is assumed to be north, which is often close enough for a first pass and always visibly
 * wrong on the traced result if it is not: a wrong guess here distorts every building on the
 * site identically and is obvious the moment the trace is previewed, not a silent error.
 */

import type { Ground } from '@/types';

export interface ImagePoint {
  x: number;
  y: number;
}

export interface Calibration {
  /** The two points the operator clicked, in image pixels. */
  pointA: ImagePoint;
  pointB: ImagePoint;
  /** The real-world distance between them, metres, as the operator entered it. */
  realDistanceMetres: number;
  /** Where image-pixel (originPixel) maps to ENU (0, 0) — typically the site datum. */
  originPixel: ImagePoint;
  /**
   * Compass bearing, radians clockwise from north, that image "up" (−y) points toward.
   * Zero — the default — means north-up.
   */
  northBearing: number;
}

export function metresPerPixel(calibration: Calibration): number {
  const dx = calibration.pointB.x - calibration.pointA.x;
  const dy = calibration.pointB.y - calibration.pointA.y;
  const pixelDistance = Math.hypot(dx, dy);
  if (pixelDistance === 0) return 0;
  return calibration.realDistanceMetres / pixelDistance;
}

/**
 * The pixel→ground transform, derived once and reused by both directions below.
 *
 * Image space is y-down; ENU is y-north — a reflection, not just a rotation. Decomposing
 * into "image up" and "image right" as compass unit vectors and projecting onto them keeps
 * the sign of every term traceable, rather than trusting a rotation matrix assembled by eye:
 *
 *   up direction, as an (e, n) unit vector at bearing θ = northBearing:      (sin θ,  cos θ)
 *   right direction, 90° clockwise from up, at bearing θ + 90°:             (cos θ, −sin θ)
 *
 * A point `xImg` pixel-metres right and `yImg` pixel-metres down (y-down) sits `xImg` along
 * "right" and `−yImg` along "up" (down is negative up), giving:
 *
 *   e =  xImg·cos θ − yImg·sin θ
 *   n = −xImg·sin θ − yImg·cos θ
 *
 * That matrix [[cos θ, −sin θ], [−sin θ, −cos θ]] has determinant −1 (a reflection composed
 * with a rotation) and squares to the identity, so it is its own inverse — the same formula
 * converts ground metres back to image metres by swapping which pair of variables is which.
 */
function rotateImageToGround(xImg: number, yImg: number, northBearing: number): Ground {
  const cos = Math.cos(northBearing);
  const sin = Math.sin(northBearing);
  return {
    e: xImg * cos - yImg * sin,
    n: -xImg * sin - yImg * cos,
  };
}

export function pixelToGround(point: ImagePoint, calibration: Calibration): Ground {
  const scale = metresPerPixel(calibration);
  const xImg = (point.x - calibration.originPixel.x) * scale;
  const yImg = (point.y - calibration.originPixel.y) * scale;
  return rotateImageToGround(xImg, yImg, calibration.northBearing);
}

/** Inverse of `pixelToGround` — used to draw the calibration's own reference points back
 *  onto the image after a reload, or to check a round trip. */
export function groundToPixel(point: Ground, calibration: Calibration): ImagePoint {
  const scale = metresPerPixel(calibration);
  if (scale === 0) return calibration.originPixel;

  // The transform is its own inverse (see rotateImageToGround) — same formula, e and n in
  // place of xImg and yImg.
  const { e: xImg, n: yImg } = rotateImageToGround(point.e, point.n, calibration.northBearing);

  return {
    x: calibration.originPixel.x + xImg / scale,
    y: calibration.originPixel.y + yImg / scale,
  };
}

/** A polygon traced in image pixels, converted to a closed ground polygon. */
export function tracePolygonToGround(points: ImagePoint[], calibration: Calibration): Ground[] {
  return points.map((p) => pixelToGround(p, calibration));
}
