/**
 * The one place ENU meets three.js.
 *
 * Domain positions are East-North-Up, because that is what a tracking system produces.
 * three.js is Y-up with -Z conventionally forward. Converting in one file rather than at
 * every call site is what keeps the rest of the renderer readable and stops a sign error
 * in one component from being invisible next to a correct one somewhere else.
 *
 *   world.x =  east
 *   world.y =  up
 *   world.z = -north
 */

import { Vector3 } from 'three';
import type { Enu, Ground } from '@/types';

export type WorldTuple = [number, number, number];

export function toWorld(p: Enu): WorldTuple {
  return [p.e, p.u, -p.n];
}

export function groundToWorld(g: Ground, u = 0): WorldTuple {
  return [g.e, u, -g.n];
}

/** Write into an existing vector. Used in per-frame code, where allocation is not free. */
export function writeWorld(p: Enu, target: Vector3): Vector3 {
  return target.set(p.e, p.u, -p.n);
}

export function toVector3(p: Enu): Vector3 {
  return new Vector3(p.e, p.u, -p.n);
}

/**
 * Y-rotation that points an object's local +Z along a domain heading.
 *
 * Domain heading is radians clockwise from north; north is -Z in world space.
 */
export function yawFor(heading: number): number {
  return Math.PI - heading;
}

/** Domain bearing of a world-space direction. Inverse of `yawFor`'s intent. */
export function headingFromWorld(x: number, z: number): number {
  const h = Math.atan2(x, -z);
  return h < 0 ? h + Math.PI * 2 : h;
}
