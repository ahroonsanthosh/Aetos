/**
 * Screen-space label decluttering.
 *
 * Labels are anchored to things in a 3D scene, so their screen positions are decided by the
 * camera rather than by a layout engine. Any two anchors can project to the same few pixels,
 * and at the substation — where a 10/10 asset, two transformers, a camera mast and an RF mast
 * sit inside 120 metres — they routinely do. Left alone the labels stack into an unreadable
 * pile, which is worse than showing fewer: the operator loses the one they needed among the
 * ones they did not.
 *
 * So this is a cartographer's solution rather than a graphics one. Every frame, all labels are
 * projected, sorted by how much they matter, and placed greedily: the most important label
 * takes its space, and anything that would overlap already-claimed space is dropped. Nothing
 * is nudged, because a label that has been pushed away from its anchor is a label pointing at
 * the wrong thing.
 *
 * A dropped label is not lost information. Its asset still carries a ground ring, its track
 * still carries a marker, and selecting either brings the label back at top priority.
 *
 * One arbiter for the whole scene, deliberately: asset labels and track labels compete for the
 * same pixels, so resolving them in separate passes would just move the collision.
 */

import type { Camera } from 'three';
import { Vector3 } from 'three';

/**
 * Priority bands. Higher wins the pixels.
 *
 * Tracks outrank assets throughout: an asset is where it has always been and the operator can
 * find it again, whereas a track is the thing that changed.
 */
export const LabelPriority = {
  /** Whatever the operator has selected always wins. */
  SELECTED: 10_000,

  TRACK_CRITICAL: 900,
  TRACK_HIGH: 800,
  TRACK_MEDIUM: 700,
  TRACK_LOW: 600,

  /** A sensor that has failed is news, so it outranks the site furniture. */
  SENSOR_OFFLINE: 520,
  SENSOR_DEGRADED: 480,

  /** An asset under threat, plus its criticality. */
  ASSET_THREATENED: 300,
  /** An asset at rest: criticality alone, scaled. */
  ASSET_BASE: 0,

  SENSOR_NOMINAL: 120,
} as const;

interface Entry {
  priority: number;
  /** World-space anchor. Owners mutate this in place each frame. */
  world: Vector3;
  element: HTMLElement | null;
  /** Cached measured size, px. Re-measured periodically rather than every frame. */
  width: number;
  height: number;
  visible: boolean;
}

export interface LabelHandle {
  /** Mutate this in place to move the anchor. */
  readonly world: Vector3;
  setPriority(priority: number): void;
  setElement(element: HTMLElement | null): void;
  /**
   * Enter the label into the placement pass. Safe to call repeatedly.
   *
   * Separate from `acquireLabel` because React Strict Mode mounts, cleans up and mounts
   * again: registering at creation and removing on cleanup would leave a live component
   * holding a handle that is no longer in the pass, and its label would stop decluttering
   * for the rest of the session — in development only, which is the worst place for a bug
   * like that to hide.
   */
  attach(): void;
  release(): void;
}

const entries = new Map<symbol, Entry>();

/** Gap kept between label boxes, px. Touching labels read as one block. */
const PADDING = 6;

/** Re-measure label boxes every N passes; content changes width as figures change. */
const REMEASURE_INTERVAL = 20;

let passCount = 0;

export function acquireLabel(priority: number): LabelHandle {
  const key = Symbol('label');
  const entry: Entry = {
    priority,
    world: new Vector3(),
    element: null,
    width: 0,
    height: 0,
    visible: true,
  };
  return {
    world: entry.world,
    attach() {
      entries.set(key, entry);
    },
    setPriority(next) {
      entry.priority = next;
    },
    setElement(element) {
      entry.element = element;
      // Force a re-measure: a new element may carry different content.
      entry.width = 0;
    },
    release() {
      entries.delete(key);
    },
  };
}

const ndc = new Vector3();

interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

function overlaps(a: Rect, b: Rect): boolean {
  return a.x0 < b.x1 && a.x1 > b.x0 && a.y0 < b.y1 && a.y1 > b.y0;
}

/**
 * Run one placement pass. Called once per frame, after everything has moved.
 */
export function declutter(camera: Camera, width: number, height: number): void {
  if (entries.size === 0) return;

  const remeasure = passCount++ % REMEASURE_INTERVAL === 0;

  // Sorted by priority so the greedy placement below is a priority order, not an accident
  // of React mount order.
  const ordered = [...entries.values()].sort((a, b) => b.priority - a.priority);
  const claimed: Rect[] = [];

  for (const entry of ordered) {
    const element = entry.element;
    if (!element) continue;

    if (remeasure || entry.width === 0) {
      entry.width = element.offsetWidth;
      entry.height = element.offsetHeight;
    }

    ndc.copy(entry.world).project(camera);

    // z beyond the far plane means the anchor is behind the camera; projected x and y are
    // meaningless there and would place a phantom box in the middle of the screen.
    let show = ndc.z <= 1;
    let rect: Rect | null = null;

    if (show) {
      const x = (ndc.x * 0.5 + 0.5) * width;
      const y = (-ndc.y * 0.5 + 0.5) * height;
      const halfWidth = entry.width / 2 + PADDING;
      const halfHeight = entry.height / 2 + PADDING;

      rect = { x0: x - halfWidth, y0: y - halfHeight, x1: x + halfWidth, y1: y + halfHeight };

      // Fully off-screen labels are dropped without claiming space, so they cannot block a
      // label that is actually visible.
      if (rect.x1 < 0 || rect.x0 > width || rect.y1 < 0 || rect.y0 > height) {
        show = false;
      } else {
        for (const other of claimed) {
          if (overlaps(rect, other)) {
            show = false;
            break;
          }
        }
      }
    }

    if (show && rect) claimed.push(rect);

    // Written only on change: touching style every frame for every label is a layout
    // invalidation the browser does not need.
    if (show !== entry.visible) {
      entry.visible = show;
      element.style.opacity = show ? '1' : '0';
      element.style.pointerEvents = show ? 'auto' : 'none';
    }
  }
}

/** Test seam. Clears all registrations. */
export function resetLabels(): void {
  entries.clear();
  passCount = 0;
}
