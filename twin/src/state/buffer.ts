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
 * The buffer renders one tick in the past, so it is always interpolating between two
 * known positions rather than extrapolating past the newest one. That costs 100 ms of
 * latency and buys motion that never overshoots and never stutters on a late packet.
 */

import type { Enu, Snapshot, Track } from '@/types';
import { lerpEnu } from '@/lib/geometry';

interface Frame {
  snapshot: Snapshot;
  /** `performance.now()` when this snapshot was received. */
  receivedAt: number;
}

let previous: Frame | null = null;
let current: Frame | null = null;

/** Playback delay, milliseconds. One snapshot interval at 10 Hz. */
const DELAY_MS = 100;

export function pushSnapshot(snapshot: Snapshot): void {
  const frame: Frame = { snapshot, receivedAt: performance.now() };
  previous = current;
  current = frame;
}

export function resetBuffer(): void {
  previous = null;
  current = null;
}

/** The newest snapshot, for anything that does not need smoothing. */
export function latestSnapshot(): Snapshot | null {
  return current?.snapshot ?? null;
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

  const renderTime = performance.now() - DELAY_MS;
  const span = current.receivedAt - previous.receivedAt;
  if (span <= 0) return { a: current.snapshot, b: current.snapshot, t: 0 };

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

/** Smoothed heading, taking the shorter way round the circle. */
export function interpolatedHeading(trackId: string): number | null {
  const s = sample();
  if (!s) return null;

  const from = s.a.tracks.find((t) => t.trackId === trackId);
  const to = s.b.tracks.find((t) => t.trackId === trackId);
  if (!to) return null;
  if (!from) return to.heading;

  let delta = to.heading - from.heading;
  while (delta > Math.PI) delta -= Math.PI * 2;
  while (delta < -Math.PI) delta += Math.PI * 2;
  return from.heading + delta * s.t;
}

/** Tracks in the newest snapshot. Identity and threat do not need interpolating. */
export function currentTracks(): Track[] {
  return current?.snapshot.tracks ?? [];
}
