/**
 * Snapshot interpolation buffer.
 *
 * Snapshots arrive at 10 Hz. The scene renders at 60. Something has to bridge that, and
 * it must not be React — re-rendering the 3D tree sixty times a second to move a track is
 * how these interfaces become unusable.
 *
 * So this module holds the last two snapshots outside React entirely. `useFrame` reads
 * positions from here every frame at zero allocation cost, and React only re-renders when
 * something an operator would actually read has changed.
 *
 * The buffer renders one interval in the past, so it is always interpolating between two
 * known positions rather than extrapolating past the newest one. That costs one interval of
 * latency (100 ms simulated, about a second live) and buys motion that never overshoots and
 * never stutters on a late packet.
 */

import type { Enu, Snapshot } from '@/types';
import { lerpEnu } from '@/lib/geometry';

interface Frame {
  snapshot: Snapshot;
  /** `performance.now()` when this snapshot was received. */
  receivedAt: number;
}

let previous: Frame | null = null;
let current: Frame | null = null;

/** Smallest playback delay, milliseconds. The delay is otherwise one measured interval. */
const MIN_DELAY_MS = 100;

export function pushSnapshot(snapshot: Snapshot): void {
  const frame: Frame = { snapshot, receivedAt: performance.now() };
  previous = current;
  current = frame;
}

export function resetBuffer(): void {
  previous = null;
  current = null;
}

/**
 * Interpolation factor and the two frames straddling the render clock.
 *
 * Returns undefined until two snapshots have arrived, which is the only moment the scene
 * has nothing to draw.
 */
function sample(): { a: Snapshot; b: Snapshot; t: number } | undefined {
  if (!current) return undefined;
  if (!previous) return { a: current.snapshot, b: current.snapshot, t: 0 };

  const span = current.receivedAt - previous.receivedAt;
  if (span <= 0) return { a: current.snapshot, b: current.snapshot, t: 0 };

  // One measured interval behind, not a fixed 100 ms: the simulator publishes at 10 Hz but the
  // live Aetos feed about once a second, and a fixed delay would make live tracks jump most of
  // the way to each new position the moment it arrived. At 10 Hz this is the same 100 ms.
  const renderTime = performance.now() - Math.max(span, MIN_DELAY_MS);
  const t = (renderTime - previous.receivedAt) / span;
  return {
    a: previous.snapshot,
    b: current.snapshot,
    // Clamped: a late snapshot must not cause the scene to extrapolate a track forward
    // past anything the source actually reported.
    t: t < 0 ? 0 : t > 1 ? 1 : t,
  };
}

const scratch: Enu = { e: 0, n: 0, u: 0 };

/**
 * Smoothed position of one track, written into a reusable object.
 *
 * Returns null when the track is not in both frames — a track that has just appeared or
 * just dropped is drawn at its reported position rather than interpolated toward one that
 * does not exist.
 */
export function interpolatedPosition(trackId: string): Enu | null {
  const s = sample();
  if (!s) return null;

  const from = s.a.tracks.find((t) => t.trackId === trackId);
  const to = s.b.tracks.find((t) => t.trackId === trackId);

  if (!to) return null;
  if (!from) return to.position;

  const p = lerpEnu(from.position, to.position, s.t);
  scratch.e = p.e;
  scratch.n = p.n;
  scratch.u = p.u;
  return scratch;
}
