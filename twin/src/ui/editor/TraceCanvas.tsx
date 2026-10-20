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
 * `ViewTransform` the operator pans and zooms. The image is drawn rotated by the
 * calibration's bearing about its own centre — a screenshot is almost never north-up, and a
 * traced building only lines up with the real one if what you see matches what you traced.
 *
 * Trace modes (`trace-boundary` / `trace-asset`) work identically in both: click to add a
 * vertex, double-click, Enter, or "Finish shape" to close, Escape to cancel.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import type { Ground } from '@/types';
import { palette } from '@/lib/palette';
import { metresPerPixel, pixelToGround, type Calibration, type ImagePoint } from '@/site/import/calibration';
import { boundingBoxAround, enuToLatLon, type LatLon } from '@/site/import/geo';
import { tileBoundsEnu, tilesForBounds, zoomForScale } from '@/site/import/tiles';
import { getTile } from './basemap';
import {
  enuToScreen,
  panByPixels,
  scaleBarFor,
  screenToEnu,
  zoomAt,
  type ViewTransform,
} from './viewTransform';

/** Pointer travel, px, beyond which a press is a drag rather than a click. */
const DRAG_THRESHOLD_PX = 4;

export type TraceMode =
  | 'view'
  | 'calibrate'
  | 'position-image'
  | 'trace-boundary'
  | 'trace-asset'
  | 'place-asset';

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
  /** Draw the image fully opaque instead of see-through, so its own edge content can be
   *  told apart from the map showing through it. */
  imageFullOpacity: boolean;
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

  /* --- Editing the selected shape ---------------------------------------------------
   * All four take and return absolute ground coordinates; the caller owns the asset maths.
   * A trace is dozens of clicks, and before these existed a misplaced corner meant
   * deleting the whole thing and starting again.
   */
  /** Translate the whole asset by a ground delta. */
  onMoveAsset: (id: string, deltaE: number, deltaN: number) => void;
  /** Move one vertex to an absolute ground point. */
  onMoveVertex: (id: string, vertexIndex: number, point: Ground) => void;
  /** Insert a new vertex immediately after `afterIndex`. */
  onInsertVertex: (id: string, afterIndex: number, point: Ground) => void;
  /** Remove a vertex. Ignored by the caller if it would leave fewer than three. */
  onDeleteVertex: (id: string, vertexIndex: number) => void;
  /** Translate the calibrated background image by a ground delta — `position-image` mode. */
  onMoveImage: (deltaE: number, deltaN: number) => void;
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
  imageFullOpacity,
  onCalibrated,
  boundary,
  shapes,
  selectedId,
  onSelect,
  onTraceComplete,
  onPlace,
  datum,
  showBasemap,
  onMoveAsset,
  onMoveVertex,
  onInsertVertex,
  onDeleteVertex,
  onMoveImage,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [tracePoints, setTracePoints] = useState<Ground[]>([]);
  const [calibrationClick, setCalibrationClick] = useState<ImagePoint | null>(null);
  const [cursor, setCursor] = useState<Ground | null>(null);
  /**
   * A pointer press that has not yet been classified.
   *
   * Left-drag pans in every mode and a left *click* performs the mode's action, so which
   * of the two a press turns out to be is only known on release — after seeing whether the
   * pointer travelled. Deciding on press instead would mean either no dragging or no
   * clicking, and the map needs both.
   */
  const dragRef = useRef<{
    x: number;
    y: number;
    view: ViewTransform;
    moved: boolean;
    pans: boolean;
    /** Set when the press landed on the selected shape or one of its handles, in which
     *  case the drag edits geometry instead of moving the camera. */
    edit?:
      | { kind: 'vertex'; id: string; index: number }
      | { kind: 'body'; id: string; lastE: number; lastN: number }
      | { kind: 'image'; lastE: number; lastN: number };
    /** The press already did its work on the way down — suppress the click action. */
    consumed?: boolean;
  } | null>(null);
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
      drawCalibratedImage(ctx, backgroundImage, imageCalibration, view, imageFullOpacity, toScreen);
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

    // Handles on the selected shape, so a wrong trace can be corrected rather than redone.
    // Only in select mode: they would compete with the click that adds a trace vertex.
    if (mode === 'view' && selectedId) {
      const shape = shapes.find((sh) => sh.id === selectedId);
      if (shape) drawVertexHandles(ctx, shape.polygon.map(toScreen));
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

    // Last, over everything: without it, "how big is this" is guesswork at every zoom.
    drawScaleBar(ctx, view, width, height);
  }, [
    width, height, view, mode, backgroundImage, imageCalibration, imageFullOpacity, calibrationClick,
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

    // In select mode, a press on the selected shape's handles or body edits it rather than
    // moving the camera — the ordinary direct-manipulation contract, and the reason a
    // misplaced corner no longer means deleting the whole trace.
    if (mode === 'view' && selectedId && e.button === 0) {
      const shape = shapes.find((sh) => sh.id === selectedId);
      if (shape) {
        const hit = hitHandles(screen, shape.polygon.map(toScreen));

        if (hit?.kind === 'vertex') {
          if (e.altKey) {
            // Alt-click removes a corner. The caller refuses if it would leave fewer
            // than three, so a shape can never be edited into something undrawable.
            onDeleteVertex(shape.id, hit.index);
            dragRef.current = { x: e.clientX, y: e.clientY, view, moved: false, pans: false, consumed: true };
            canvasRef.current?.setPointerCapture(e.pointerId);
            return;
          }
          dragRef.current = {
            x: e.clientX, y: e.clientY, view, moved: false, pans: false,
            edit: { kind: 'vertex', id: shape.id, index: hit.index },
          };
          canvasRef.current?.setPointerCapture(e.pointerId);
          return;
        }

        if (hit?.kind === 'midpoint') {
          // Dragging an edge midpoint inserts a corner there and then moves it, which is
          // how a trace that missed a corner gets one without starting over.
          onInsertVertex(shape.id, hit.index, toGround(screen));
          dragRef.current = {
            x: e.clientX, y: e.clientY, view, moved: false, pans: false,
            edit: { kind: 'vertex', id: shape.id, index: hit.index + 1 },
          };
          canvasRef.current?.setPointerCapture(e.pointerId);
          return;
        }

        if (pointInPolygon(toGround(screen), shape.polygon)) {
          const ground = toGround(screen);
          dragRef.current = {
            x: e.clientX, y: e.clientY, view, moved: false, pans: false,
            edit: { kind: 'body', id: shape.id, lastE: ground.e, lastN: ground.n },
          };
          canvasRef.current?.setPointerCapture(e.pointerId);
          return;
        }
      }
    }

    // Every left-drag in this mode moves the image, full stop — no hit-test against its
    // current bounds. A drag that has to land exactly on the image is a drag that
    // sometimes misses and silently pans the whole view instead, which looks identical to
    // a successful move (the image slides across the screen either way) right up until the
    // operator notices it never actually got any closer to the real building. Panning is
    // still available: middle- or right-drag, unaffected by this branch since it only
    // claims button 0.
    if (mode === 'position-image' && e.button === 0 && backgroundImage && imageCalibration) {
      const ground = toGround(screen);
      dragRef.current = {
        x: e.clientX, y: e.clientY, view, moved: false, pans: false,
        edit: { kind: 'image', lastE: ground.e, lastN: ground.n },
      };
      canvasRef.current?.setPointerCapture(e.pointerId);
      return;
    }

    // Placing a single asset or clicking down a trace vertex is a precise action, not a
    // navigation gesture — the left button never pans here, the same reasoning as aligning
    // the image above. Without this, an ordinary hand's few pixels of jitter between press
    // and release nudged the whole view (dragging the map, and everything drawn on it,
    // along with the cursor) while also cancelling the click that was supposed to happen —
    // a building that silently failed to appear where clicked, on a map that had quietly
    // moved a little. Panning stays available on the middle or right button, unaffected by
    // this branch since it only claims button 0.
    if (
      (mode === 'trace-boundary' || mode === 'trace-asset' || mode === 'place-asset') &&
      e.button === 0
    ) {
      dragRef.current = { x: e.clientX, y: e.clientY, view, moved: false, pans: false };
      canvasRef.current?.setPointerCapture(e.pointerId);
      return;
    }

    // Every other press starts as a candidate: it becomes a pan if the pointer travels, and
    // the mode's own action if it does not. Classified on release, in `onPointerUp`.
    dragRef.current = { x: e.clientX, y: e.clientY, view, moved: false, pans: true };
    canvasRef.current?.setPointerCapture(e.pointerId);
    if (e.button === 1 || e.button === 2) e.preventDefault();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    const screen = { x: e.clientX - rect.left, y: e.clientY - rect.top };

    const drag = dragRef.current;
    if (drag) {
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;

      // A few pixels of slop, so a click with a shaky hand is still a click rather than a
      // one-pixel pan that swallows the action.
      if (!drag.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD_PX) drag.moved = true;
      if (!drag.moved) return;

      if (drag.edit?.kind === 'vertex') {
        onMoveVertex(drag.edit.id, drag.edit.index, toGround(screen));
        return;
      }

      if (drag.edit?.kind === 'body') {
        // Translate by the ground movement since the last frame, rather than from the
        // press point: the asset moves under the pointer without accumulating drift.
        const ground = toGround(screen);
        onMoveAsset(drag.edit.id, ground.e - drag.edit.lastE, ground.n - drag.edit.lastN);
        drag.edit.lastE = ground.e;
        drag.edit.lastN = ground.n;
        return;
      }

      if (drag.edit?.kind === 'image') {
        const ground = toGround(screen);
        onMoveImage(ground.e - drag.edit.lastE, ground.n - drag.edit.lastN);
        drag.edit.lastE = ground.e;
        drag.edit.lastN = ground.n;
        return;
      }

      if (drag.pans) onViewChange(panByPixels(drag.view, dx, dy));
      return;
    }

    if (mode === 'trace-boundary' || mode === 'trace-asset') {
      setCursor(toGround(screen));
    }
  };

  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    dragRef.current = null;
    canvasRef.current?.releasePointerCapture(e.pointerId);

    // An edit, or a press that already did its work on the way down, never carries a second
    // action on release. Middle and right buttons pan exclusively, so they never act either.
    if (!drag || drag.consumed || drag.edit || e.button !== 0) return;

    // Everywhere else, a drag that moved is a pan, not a click, and should not also act —
    // except trace/place modes, whose presses never pan in the first place (see
    // `onPointerDown`), so a few pixels of hand jitter is still just a click there.
    const clickOnlyMode = mode === 'trace-boundary' || mode === 'trace-asset' || mode === 'place-asset';
    if (!clickOnlyMode && drag.moved) return;

    const rect = canvasRef.current!.getBoundingClientRect();
    const screen = { x: e.clientX - rect.left, y: e.clientY - rect.top };

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
    const rect = canvasRef.current!.getBoundingClientRect();
    const anchor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    // Anchored on the cursor: the thing you point at is the thing that stays put.
    onViewChange(zoomAt(view, Math.exp(e.deltaY * 0.001), anchor, width, height));
  };

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      style={{
        cursor: mode === 'view' ? 'default' : mode === 'position-image' ? 'move' : 'crosshair',
        touchAction: 'none',
      }}
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
  view: ViewTransform,
  fullOpacity: boolean,
  toScreen: (p: Ground) => { x: number; y: number },
) {
  // Rotate about the image's own centre rather than project all four corners: canvas
  // `rotate()` turns clockwise for a positive angle in a y-down space, which is exactly what
  // `northBearing` already means (clockwise from north) — so the transform below and the
  // ground maths in calibration.ts agree without needing a second, independent rotation.
  const centreImg = { x: image.width / 2, y: image.height / 2 };
  const centreScreen = toScreen(pixelToGround(centreImg, calibration));

  // Screen pixels per image pixel: how many ground metres one image pixel covers, divided by
  // how many ground metres one screen pixel covers at the current zoom.
  const scale = metresPerPixel(calibration) / view.metresPerPixel;
  const drawWidth = image.width * scale;
  const drawHeight = image.height * scale;

  ctx.save();
  ctx.translate(centreScreen.x, centreScreen.y);
  ctx.rotate(calibration.northBearing);

  ctx.globalAlpha = fullOpacity ? 1 : 0.75;
  ctx.drawImage(image, -drawWidth / 2, -drawHeight / 2, drawWidth, drawHeight);

  // A crisp, full-opacity frame around it — without one, a faded photo blended over real
  // map tiles has no visible edge, and "where does my upload end and the real map begin"
  // is not a question the operator should have to answer by eye while dragging it.
  ctx.globalAlpha = 1;
  ctx.strokeStyle = palette.select;
  ctx.lineWidth = 1.5;
  ctx.strokeRect(-drawWidth / 2, -drawHeight / 2, drawWidth, drawHeight);

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

/** A labelled bar in the bottom-left corner giving the current scale in metres. */
/** Hit radii, px. Vertices win over midpoints where the two overlap on a short edge. */
const VERTEX_HIT_PX = 9;
const MIDPOINT_HIT_PX = 7;

/**
 * What, if anything, a press landed on among the selected shape's editing handles.
 *
 * Vertices are tested before midpoints: on a short edge the two are close together, and
 * moving an existing corner is the more common intent than inserting a new one.
 */
function hitHandles(
  screen: { x: number; y: number },
  polygon: { x: number; y: number }[],
): { kind: 'vertex' | 'midpoint'; index: number } | null {
  for (let i = 0; i < polygon.length; i++) {
    const p = polygon[i]!;
    if (Math.hypot(screen.x - p.x, screen.y - p.y) <= VERTEX_HIT_PX) {
      return { kind: 'vertex', index: i };
    }
  }

  for (let i = 0; i < polygon.length; i++) {
    const a = polygon[i]!;
    const b = polygon[(i + 1) % polygon.length]!;
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    if (Math.hypot(screen.x - mid.x, screen.y - mid.y) <= MIDPOINT_HIT_PX) {
      return { kind: 'midpoint', index: i };
    }
  }

  return null;
}

/**
 * Corner handles filled, edge midpoints hollow.
 *
 * The difference is the affordance: a filled handle is a thing that exists and can be
 * moved, a hollow one is a place where a corner could be added. Drawing them identically
 * would make "insert" and "move" indistinguishable until after the drag.
 */
function drawVertexHandles(ctx: CanvasRenderingContext2D, polygon: { x: number; y: number }[]) {
  ctx.save();

  for (let i = 0; i < polygon.length; i++) {
    const a = polygon[i]!;
    const b = polygon[(i + 1) % polygon.length]!;

    // Midpoints are only offered where the edge has room for a handle that is not sitting
    // on top of the corners either side of it.
    if (Math.hypot(b.x - a.x, b.y - a.y) > VERTEX_HIT_PX * 4) {
      ctx.beginPath();
      ctx.arc((a.x + b.x) / 2, (a.y + b.y) / 2, 3.5, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(216, 233, 242, 0.55)';
      ctx.lineWidth = 1.2;
      ctx.stroke();
    }
  }

  for (const p of polygon) {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = palette.select;
    ctx.fill();
    ctx.strokeStyle = 'rgba(4, 9, 14, 0.9)';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  ctx.restore();
}

function drawScaleBar(
  ctx: CanvasRenderingContext2D,
  view: ViewTransform,
  width: number,
  height: number,
) {
  const { metres, pixels } = scaleBarFor(view, width);
  const x = 14;
  const y = height - 20;

  ctx.save();
  ctx.strokeStyle = 'rgba(216, 233, 242, 0.75)';
  ctx.fillStyle = 'rgba(216, 233, 242, 0.85)';
  ctx.lineWidth = 1.5;

  ctx.beginPath();
  ctx.moveTo(x, y - 5);
  ctx.lineTo(x, y);
  ctx.lineTo(x + pixels, y);
  ctx.lineTo(x + pixels, y - 5);
  ctx.stroke();

  ctx.font = '10px ui-monospace, monospace';
  ctx.textBaseline = 'bottom';
  ctx.fillText(metres >= 1000 ? `${metres / 1000} km` : `${metres} m`, x, y - 7);
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
