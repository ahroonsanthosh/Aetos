'use client';

/**
 * The editor's 2D top-down drawing surface.
 *
 * Two distinct rendering modes, not one transform doing double duty:
 *
 * **Calibrating** — before an image has a real-world scale, "where is it in ENU" is not yet
 * a meaningful question. So the image is shown fit-to-canvas in its own raw pixel space,
 * independent of the ENU view, and the two reference clicks are recorded as raw image
 * pixels. Only once a real-world distance is supplied does the image acquire an ENU
 * position at all.
 *
 * **Editing** — everything (the calibrated image, the boundary, every traced or imported
 * building) is positioned in ENU metres and projected to the screen through the one
 * `ViewTransform` the operator pans and zooms. The image is placed north-up: a documented
 * simplification (see below), not an error in the underlying maths, which still applies a
 * calibration's full rotation when interpreting a *traced point* — only the raster preview
 * itself stays axis-aligned.
 *
 * Trace modes (`trace-boundary` / `trace-asset`) work identically in both: click to add a
 * vertex, double-click, Enter, or "Finish shape" to close, Escape to cancel.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import type { Ground } from '@/types';
import { palette } from '@/lib/palette';
import { pixelToGround, type Calibration, type ImagePoint } from '@/site/import/calibration';
import { boundingBoxAround, enuToLatLon, type LatLon } from '@/site/import/geo';
import { tileBoundsEnu, tilesForBounds, zoomForScale } from '@/site/import/tiles';
import { getTile } from './basemap';
import { enuToScreen, screenToEnu, type ViewTransform } from './viewTransform';

export type TraceMode = 'view' | 'calibrate' | 'trace-boundary' | 'trace-asset' | 'place-asset';

export interface TraceShape {
  id: string;
  polygon: Ground[];
  colour: string;
  emphasis?: boolean;
}

interface Props {
  width: number;
  height: number;
  view: ViewTransform;
  onViewChange: (view: ViewTransform) => void;
  mode: TraceMode;
  backgroundImage: HTMLImageElement | null;
  /** Present once the image has a real-world scale; drives ENU-space rendering. */
  imageCalibration: Calibration | null;
  /** Fired with two RAW IMAGE PIXEL points once the operator has clicked both. The caller
   *  asks for the real-world distance and builds the `Calibration`. */
  onCalibrated: (pointA: ImagePoint, pointB: ImagePoint) => void;
  boundary: Ground[];
  shapes: TraceShape[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onTraceComplete: (polygon: Ground[]) => void;
  /** Fired in `place-asset` mode: a single click, not a traced polygon — for a small,
   *  roughly square asset (a gate, a transformer) that doesn't warrant tracing its outline. */
  onPlace: (point: Ground) => void;
  /** The site datum, so ENU can be converted back to lat/lon for tile lookup. */
  datum: LatLon;
  /** Draw real map tiles under everything. The single change that turns the editor from
   *  "type coordinates and hope" into something you can actually aim. */
  showBasemap: boolean;
}

/** Fit an image inside a canvas, centred, without cropping — the calibration-mode layout. */
function fitImageToCanvas(image: HTMLImageElement, canvasWidth: number, canvasHeight: number) {
  const scale = Math.min(canvasWidth / image.width, canvasHeight / image.height);
  const drawWidth = image.width * scale;
  const drawHeight = image.height * scale;
  return {
    scale,
    offsetX: (canvasWidth - drawWidth) / 2,
    offsetY: (canvasHeight - drawHeight) / 2,
    drawWidth,
    drawHeight,
  };
}

export function TraceCanvas({
  width,
  height,
  view,
  onViewChange,
  mode,
  backgroundImage,
  imageCalibration,
  onCalibrated,
  boundary,
  shapes,
  selectedId,
  onSelect,
  onTraceComplete,
  onPlace,
  datum,
  showBasemap,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [tracePoints, setTracePoints] = useState<Ground[]>([]);
  const [calibrationClick, setCalibrationClick] = useState<ImagePoint | null>(null);
  const [cursor, setCursor] = useState<Ground | null>(null);
  const dragRef = useRef<{ x: number; y: number; view: ViewTransform } | null>(null);
  // Bumped when a tile finishes loading, purely to re-run the draw effect below.
  const [tileTick, setTileTick] = useState(0);

  // A mode switch abandons whatever was mid-trace or mid-calibration — starting to draw a
  // building and then choosing "select" instead should not leave a half-built polygon live.
  useEffect(() => {
    setTracePoints([]);
    setCalibrationClick(null);
  }, [mode]);

  const toScreen = useCallback((p: Ground) => enuToScreen(p, view, width, height), [view, width, height]);
  const toGround = useCallback(
    (p: { x: number; y: number }) => screenToEnu(p, view, width, height),
    [view, width, height],
  );

  // --- Drawing ---------------------------------------------------------------------------

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;

    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#050a0f';
    ctx.fillRect(0, 0, width, height);

    if (mode === 'calibrate' && backgroundImage) {
      drawCalibrationImage(ctx, backgroundImage, width, height, calibrationClick);
      return; // Calibration is its own screen — nothing ENU-based overlaps it.
    }

    // Basemap first, under everything — it is context, not content.
    if (showBasemap) {
      drawBasemap(ctx, view, width, height, datum, toScreen, () => setTileTick((t) => t + 1));
    }

    drawGrid(ctx, view, width, height);

    if (backgroundImage && imageCalibration) {
      drawCalibratedImage(ctx, backgroundImage, imageCalibration, toScreen);
    }

    if (boundary.length >= 2) {
      drawPolygon(ctx, boundary.map(toScreen), { stroke: palette.holo, fill: 'rgba(88,216,230,0.05)', width: 1.5 });
    }

    for (const shape of shapes) {
      const screenPoints = shape.polygon.map(toScreen);
      const selected = shape.id === selectedId;
      drawPolygon(ctx, screenPoints, {
        stroke: selected ? palette.select : shape.colour,
        fill: shape.emphasis ? `${shape.colour}33` : `${shape.colour}18`,
        width: selected ? 2.5 : 1.5,
      });
    }

    if (mode === 'trace-boundary' || mode === 'trace-asset') {
      const live = cursor ? [...tracePoints, cursor] : tracePoints;
      drawPolygon(ctx, live.map(toScreen), {
        stroke: palette.caution,
        fill: 'rgba(233,178,76,0.08)',
        width: 1.5,
        dashed: true,
        closeStroke: false,
      });
      for (const p of tracePoints) drawHandle(ctx, toScreen(p), palette.caution);
    }
  }, [
    width, height, view, mode, backgroundImage, imageCalibration, calibrationClick,
    boundary, shapes, selectedId, tracePoints, cursor, toScreen,
    showBasemap, datum, tileTick,
  ]);

  // --- Interaction -------------------------------------------------------------------------

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    const screen = { x: e.clientX - rect.left, y: e.clientY - rect.top };

    if (mode === 'calibrate') {
      if (!backgroundImage) return;
      const fit = fitImageToCanvas(backgroundImage, width, height);
      const imagePoint: ImagePoint = {
        x: (screen.x - fit.offsetX) / fit.scale,
        y: (screen.y - fit.offsetY) / fit.scale,
      };
      // Clicks outside the letterboxed image are not on the image at all.
      if (imagePoint.x < 0 || imagePoint.x > backgroundImage.width) return;
      if (imagePoint.y < 0 || imagePoint.y > backgroundImage.height) return;

      if (!calibrationClick) {
        setCalibrationClick(imagePoint);
      } else {
        onCalibrated(calibrationClick, imagePoint);
        setCalibrationClick(null);
      }
      return;
    }

    // Middle-drag or right-drag pans in every other mode, including mid-trace — the
    // operator should never have to finish or cancel a shape just to reposition the view.
    if (e.button === 1 || e.button === 2) {
      dragRef.current = { x: e.clientX, y: e.clientY, view };
      e.preventDefault();
      return;
    }

    if (mode === 'trace-boundary' || mode === 'trace-asset') {
      setTracePoints((points) => [...points, toGround(screen)]);
      return;
    }

    if (mode === 'place-asset') {
      onPlace(toGround(screen));
      return;
    }

    const ground = toGround(screen);
    const hit = [...shapes].reverse().find((s) => pointInPolygon(ground, s.polygon));
    onSelect(hit?.id ?? null);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    const screen = { x: e.clientX - rect.left, y: e.clientY - rect.top };

    if (dragRef.current) {
      const dx = e.clientX - dragRef.current.x;
      const dy = e.clientY - dragRef.current.y;
      const base = dragRef.current.view;
      onViewChange({
        ...base,
        centreE: base.centreE - dx * base.metresPerPixel,
        centreN: base.centreN + dy * base.metresPerPixel,
      });
      return;
    }

    if (mode === 'trace-boundary' || mode === 'trace-asset') {
      setCursor(toGround(screen));
    }
  };

  const onPointerUp = () => {
    dragRef.current = null;
  };

  const onDoubleClick = () => {
    if (mode === 'trace-boundary' || mode === 'trace-asset') finishTrace();
  };

  const finishTrace = () => {
    if (tracePoints.length >= 3) onTraceComplete(tracePoints);
    setTracePoints([]);
    setCursor(null);
  };

  const cancelTrace = () => {
    setTracePoints([]);
    setCalibrationClick(null);
    setCursor(null);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') cancelTrace();
      if (e.key === 'Enter') finishTrace();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tracePoints]);

  const onWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    if (mode === 'calibrate') return;
    e.preventDefault();
    const factor = Math.exp(e.deltaY * 0.001);
    onViewChange({ ...view, metresPerPixel: clampScale(view.metresPerPixel * factor) });
  };

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      style={{ cursor: mode === 'view' ? 'default' : 'crosshair', touchAction: 'none' }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
      onDoubleClick={onDoubleClick}
      onWheel={onWheel}
      onContextMenu={(e) => e.preventDefault()}
    />
  );
}

function clampScale(value: number): number {
  return Math.min(200, Math.max(0.05, value));
}

function drawCalibrationImage(
  ctx: CanvasRenderingContext2D,
  image: HTMLImageElement,
  width: number,
  height: number,
  clicked: ImagePoint | null,
) {
  const fit = fitImageToCanvas(image, width, height);
  ctx.drawImage(image, fit.offsetX, fit.offsetY, fit.drawWidth, fit.drawHeight);

  if (clicked) {
    drawHandle(
      ctx,
      { x: fit.offsetX + clicked.x * fit.scale, y: fit.offsetY + clicked.y * fit.scale },
      palette.warning,
    );
  }
}

function drawCalibratedImage(
  ctx: CanvasRenderingContext2D,
  image: HTMLImageElement,
  calibration: Calibration,
  toScreen: (p: Ground) => { x: number; y: number },
) {
  // North-up placement: the image's four corners are converted through the calibration —
  // rotation included — but drawn as an axis-aligned destination rectangle. Correct only
  // when northBearing is 0, which is what every caller in this editor enforces; see the
  // file-level comment for why that trade-off is made deliberately rather than by omission.
  const topLeft = toScreen(pixelToGround({ x: 0, y: 0 }, calibration));
  const bottomRight = toScreen(pixelToGround({ x: image.width, y: image.height }, calibration));

  ctx.save();
  ctx.globalAlpha = 0.75;
  ctx.drawImage(image, topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y);
  ctx.restore();
}

/**
 * Draw whatever map tiles are currently loaded for the visible area.
 *
 * Only tiles already in the cache are drawn; anything missing is requested and the canvas
 * redraws when it arrives. The alternative — awaiting the fetches before painting — would
 * freeze the editor on every pan against a slow tile server, for a layer that is context
 * rather than content.
 *
 * Each tile is placed by converting its own lat/lon corners into ENU and projecting those,
 * so the basemap stays registered with traced geometry rather than assuming the tile grid
 * is linear in the local frame.
 */
function drawBasemap(
  ctx: CanvasRenderingContext2D,
  view: ViewTransform,
  width: number,
  height: number,
  datum: LatLon,
  toScreen: (p: Ground) => { x: number; y: number },
  onTileLoad: () => void,
) {
  // The visible ENU rectangle, expressed as a lat/lon box around its centre. The radius is
  // the half-diagonal, so the box covers the corners rather than just the axes.
  const halfWidthM = (width / 2) * view.metresPerPixel;
  const halfHeightM = (height / 2) * view.metresPerPixel;
  const centre = enuToLatLon({ e: view.centreE, n: view.centreN }, datum);
  const bounds = boundingBoxAround(centre, Math.hypot(halfWidthM, halfHeightM));

  const zoom = zoomForScale(view.metresPerPixel, centre.latitude);
  const tiles = tilesForBounds(bounds, zoom);

  ctx.save();
  ctx.globalAlpha = 0.85;
  ctx.imageSmoothingEnabled = true;

  for (const tile of tiles) {
    const image = getTile(tile, onTileLoad);
    if (!image) continue;

    const enu = tileBoundsEnu(tile, datum);
    const topLeft = toScreen({ e: enu.min.e, n: enu.max.n });
    const bottomRight = toScreen({ e: enu.max.e, n: enu.min.n });

    // +1 px on each dimension: adjacent tiles share an exact edge in ENU, but rounding to
    // device pixels can leave a hairline gap between them otherwise.
    ctx.drawImage(
      image,
      topLeft.x,
      topLeft.y,
      bottomRight.x - topLeft.x + 1,
      bottomRight.y - topLeft.y + 1,
    );
  }

  ctx.restore();
}

function drawGrid(ctx: CanvasRenderingContext2D, view: ViewTransform, width: number, height: number) {
  const spacing = niceGridSpacing(view.metresPerPixel);
  const pixelSpacing = spacing / view.metresPerPixel;
  if (pixelSpacing < 8) return;

  ctx.strokeStyle = 'rgba(88,216,230,0.07)';
  ctx.lineWidth = 1;
  ctx.beginPath();

  const originScreen = enuToScreen({ e: 0, n: 0 }, view, width, height);
  for (let x = originScreen.x % pixelSpacing; x < width; x += pixelSpacing) {
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
  }
  for (let y = originScreen.y % pixelSpacing; y < height; y += pixelSpacing) {
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
  }
  ctx.stroke();

  ctx.strokeStyle = 'rgba(88,216,230,0.35)';
  ctx.beginPath();
  ctx.moveTo(originScreen.x - 8, originScreen.y);
  ctx.lineTo(originScreen.x + 8, originScreen.y);
  ctx.moveTo(originScreen.x, originScreen.y - 8);
  ctx.lineTo(originScreen.x, originScreen.y + 8);
  ctx.stroke();
}

function niceGridSpacing(metresPerPixel: number): number {
  const target = metresPerPixel * 80;
  const ladder = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000];
  return ladder.reduce((best, c) => (Math.abs(c - target) < Math.abs(best - target) ? c : best));
}

function drawPolygon(
  ctx: CanvasRenderingContext2D,
  points: { x: number; y: number }[],
  opts: { stroke: string; fill?: string; width: number; dashed?: boolean; closeStroke?: boolean },
) {
  if (points.length < 2) return;
  ctx.save();
  ctx.setLineDash(opts.dashed ? [6, 5] : []);
  ctx.strokeStyle = opts.stroke;
  ctx.lineWidth = opts.width;
  ctx.beginPath();
  ctx.moveTo(points[0]!.x, points[0]!.y);
  for (const p of points.slice(1)) ctx.lineTo(p.x, p.y);
  if (opts.closeStroke !== false && points.length > 2) ctx.closePath();
  if (opts.fill && points.length > 2) {
    ctx.fillStyle = opts.fill;
    ctx.fill();
  }
  ctx.stroke();
  ctx.restore();
}

function drawHandle(ctx: CanvasRenderingContext2D, p: { x: number; y: number }, colour: string) {
  ctx.save();
  ctx.fillStyle = colour;
  ctx.beginPath();
  ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function pointInPolygon(p: Ground, polygon: Ground[]): boolean {
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
