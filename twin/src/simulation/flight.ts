/**
 * Flight path model for simulated objects.
 *
 * A plan is geometry plus a speed. The engine turns that into kinematics; nothing in the
 * plan states a threat, a behaviour verb it has not earned, or a position the renderer
 * could read directly. That separation is what lets the same code path later be fed by
 * real observations, which also arrive as positions rather than intentions.
 */

import type { Behaviour, Classification, Enu, Identity, TrackPoint } from '@/types';
import { clamp01, horizontalDistance, lerpEnu, TAU } from '@/lib/geometry';

export interface FlightPlan {
  id: string;
  classification: Classification;
  /** Classification confidence, 0–1. Fixed per plan in this prototype. */
  confidence: number;
  identity: Identity;
  /**
   * Set only for patterns the kinematics cannot reveal on their own — a survey grid or a
   * repeated perimeter probe. Left undefined, behaviour is derived from the motion.
   */
  declaredBehaviour?: Behaviour;
  /** Path geometry. Altitude is interpolated between waypoints. */
  waypoints: Enu[];
  /** Metres per second along the path. */
  speed: number;
  /** Restart at the first waypoint on reaching the last. */
  loop: boolean;
  /** Seconds after scenario start before this object appears. */
  startDelay: number;
  /** Sensors the simulator pretends are holding this track. */
  contributingSensors: string[];
}

export interface FlightState {
  position: Enu;
  velocity: Enu;
  speed: number;
  heading: number;
  /** True once the path has been consumed and the plan does not loop. */
  finished: boolean;
}

/** Cumulative arc lengths along a plan's waypoints, for constant-speed traversal. */
export function pathLengths(waypoints: Enu[]): { segments: number[]; total: number } {
  const segments: number[] = [];
  let total = 0;
  for (let i = 1; i < waypoints.length; i++) {
    const a = waypoints[i - 1]!;
    const b = waypoints[i]!;
    const d = Math.hypot(b.e - a.e, b.n - a.n, b.u - a.u);
    segments.push(d);
    total += d;
  }
  return { segments, total };
}

/** Position at arc length `s` along the path, clamped to the ends. */
export function positionAt(waypoints: Enu[], segments: number[], s: number): Enu {
  if (waypoints.length === 0) return { e: 0, n: 0, u: 0 };
  if (waypoints.length === 1 || s <= 0) return { ...waypoints[0]! };

  let remaining = s;
  for (let i = 0; i < segments.length; i++) {
    const len = segments[i]!;
    if (remaining <= len || i === segments.length - 1) {
      const t = len === 0 ? 0 : clamp01(remaining / len);
      return lerpEnu(waypoints[i]!, waypoints[i + 1]!, t);
    }
    remaining -= len;
  }
  return { ...waypoints[waypoints.length - 1]! };
}

/**
 * Advance a plan to `elapsed` seconds and derive kinematics.
 *
 * Velocity comes from differencing the path a short interval either side rather than from
 * the segment direction, so that a track rounding a waypoint has a velocity that turns
 * smoothly instead of snapping.
 */
export function flightStateAt(plan: FlightPlan, elapsed: number): FlightState | undefined {
  const active = elapsed - plan.startDelay;
  if (active < 0) return undefined;

  const { segments, total } = pathLengths(plan.waypoints);
  if (total === 0) return undefined;

  const travelled = active * plan.speed;
  const finished = !plan.loop && travelled >= total;
  const s = plan.loop ? travelled % total : Math.min(travelled, total);

  const position = positionAt(plan.waypoints, segments, s);

  const dt = 0.35;
  const ahead = plan.loop ? (s + plan.speed * dt) % total : Math.min(s + plan.speed * dt, total);
  const behind = plan.loop
    ? (s - plan.speed * dt + total) % total
    : Math.max(s - plan.speed * dt, 0);
  const pAhead = positionAt(plan.waypoints, segments, ahead);
  const pBehind = positionAt(plan.waypoints, segments, behind);

  const span = Math.max((ahead >= behind ? ahead - behind : total - behind + ahead) / plan.speed, 1e-3);
  const velocity: Enu = {
    e: (pAhead.e - pBehind.e) / span,
    n: (pAhead.n - pBehind.n) / span,
    u: (pAhead.u - pBehind.u) / span,
  };

  const speed = Math.hypot(velocity.e, velocity.n, velocity.u);
  let heading = Math.atan2(velocity.e, velocity.n);
  if (heading < 0) heading += TAU;

  return { position, velocity, speed, heading, finished };
}

/**
 * Straight-line extrapolation from current kinematics.
 *
 * Deliberately naive. A predictor that followed the flight plan would be clairvoyant, and
 * the interface would be demonstrating something no real system can do. Being wrong on
 * turns is the honest behaviour, and it is what the operator would actually see.
 */
export function extrapolate(
  position: Enu,
  velocity: Enu,
  now: number,
  horizonSeconds = 90,
  stepSeconds = 3,
): TrackPoint[] {
  const points: TrackPoint[] = [];
  for (let t = stepSeconds; t <= horizonSeconds; t += stepSeconds) {
    points.push({
      position: {
        e: position.e + velocity.e * t,
        n: position.n + velocity.n * t,
        // Do not let an extrapolated descent bury the track under the terrain.
        u: Math.max(position.u + velocity.u * t, 2),
      },
      t: now + t * 1000,
    });
  }
  return points;
}

/**
 * Derive what the object is doing from how it is moving.
 *
 * `declaredBehaviour` on the plan wins, because a survey grid and a perimeter probe are
 * patterns over minutes that a two-sample range trend cannot see.
 */
export function deriveBehaviour(
  plan: FlightPlan,
  position: Enu,
  velocity: Enu,
  datum: Enu = { e: 0, n: 0, u: 0 },
): Behaviour {
  if (plan.declaredBehaviour) return plan.declaredBehaviour;

  const range = horizontalDistance(position, datum);
  const horizontalSpeed = Math.hypot(velocity.e, velocity.n);

  if (horizontalSpeed < 2.5) return 'loitering';

  // Closing rate: the component of velocity along the bearing back to the datum.
  const toDatumE = (datum.e - position.e) / Math.max(range, 1e-3);
  const toDatumN = (datum.n - position.n) / Math.max(range, 1e-3);
  const closing = velocity.e * toDatumE + velocity.n * toDatumN;

  if (closing > horizontalSpeed * 0.45) return 'approaching';
  if (closing < -horizontalSpeed * 0.45) return 'departing';
  return 'transiting';
}
