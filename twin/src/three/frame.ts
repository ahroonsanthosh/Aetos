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

import type { Enu } from '@/types';

export type WorldTuple = [number, number, number];

export function toWorld(p: Enu): WorldTuple {
  return [p.e, p.u, -p.n];
}

/**
 * Y-rotation that points an object's local +Z along a domain heading.
 *
 * Domain heading is radians clockwise from north; north is -Z in world space.
 */
export function yawFor(heading: number): number {
  return Math.PI - heading;
}
