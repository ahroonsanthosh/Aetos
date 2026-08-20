/**
 * Turning domain polygons into geometry.
 *
 * Zones are defined as ground polygons with a floor and a ceiling. This is where that
 * becomes a mesh. The rotation is the fiddly part and is done once, here, so no component
 * has to reason about which way an extrusion points.
 */

import { BufferGeometry, ExtrudeGeometry, Float32BufferAttribute, Shape, Vector3 } from 'three';
import type { Ground } from '@/types';

/**
 * Extrude a ground polygon upward into a volume.
 *
 * The returned geometry has its base at local y = 0 and its top at local y = `height`,
 * which is the contract `volume.ts` relies on to place its rims and sweep. Position the
 * resulting mesh at the zone floor.
 */
export function extrudedPolygon(polygon: Ground[], height: number): BufferGeometry {
  const shape = new Shape();
  const first = polygon[0];
  if (!first) return new BufferGeometry();

  shape.moveTo(first.e, first.n);
  for (let i = 1; i < polygon.length; i++) {
    const p = polygon[i]!;
    shape.lineTo(p.e, p.n);
  }
  shape.closePath();

  const geometry = new ExtrudeGeometry(shape, {
    depth: height,
    bevelEnabled: false,
    curveSegments: 1,
  });

  // The shape is built in (e, n) and extruded along +Z. Rotating -90° about X maps
  // (e, n, d) to (e, d, -n) — precisely the ENU-to-world convention in `frame.ts`.
  geometry.rotateX(-Math.PI / 2);
  geometry.computeVertexNormals();
  return geometry;
}

/**
 * The side walls of an extruded polygon, without floor or ceiling caps.
 *
 * This is what a protected volume should actually be made of. `extrudedPolygon` closes the
 * top, and because the volumes blend additively that lid stacks on top of the front wall
 * and the back wall — three layers of fill over exactly the asset the zone exists to
 * protect, which then disappears inside its own protection. Walls alone leave the asset
 * visible through the volume, and the ceiling still reads, drawn as the rim where the
 * walls stop.
 *
 * Local y runs 0..height, matching what `volume.ts` expects.
 */
export function polygonWalls(polygon: Ground[], height: number): BufferGeometry {
  const positions: number[] = [];
  const normals: number[] = [];
  const indices: number[] = [];

  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[j]!;
    const b = polygon[i]!;

    const de = b.e - a.e;
    const dn = b.n - a.n;
    const len = Math.hypot(de, dn);
    if (len === 0) continue;

    // Outward horizontal normal for a counter-clockwise ground polygon. Purely horizontal,
    // which is what lets the material's `vertical` term treat every wall as a wall.
    const nx = dn / len;
    const nz = de / len;

    const base = positions.length / 3;

    positions.push(a.e, 0, -a.n);
    positions.push(b.e, 0, -b.n);
    positions.push(b.e, height, -b.n);
    positions.push(a.e, height, -a.n);

    for (let k = 0; k < 4; k++) normals.push(nx, 0, nz);

    indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
  }

  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
  geometry.setAttribute('normal', new Float32BufferAttribute(normals, 3));
  geometry.setIndex(indices);
  return geometry;
}

/** Closed loop of world-space points at a fixed altitude. For outlines and fence lines. */
export function polygonLoop(polygon: Ground[], u = 0): Vector3[] {
  const points = polygon.map((p) => new Vector3(p.e, u, -p.n));
  if (points.length > 0) points.push(points[0]!.clone());
  return points;
}

/** Total perimeter length of a polygon, metres. */
export function polygonPerimeter(polygon: Ground[]): number {
  let total = 0;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[j]!;
    const b = polygon[i]!;
    total += Math.hypot(b.e - a.e, b.n - a.n);
  }
  return total;
}

/**
 * Evenly spaced points around a polygon's boundary, for fence posts and marker runs.
 * Returns ground coordinates so callers can choose their own altitude.
 */
export function samplePerimeter(polygon: Ground[], spacing: number): Ground[] {
  const out: Ground[] = [];
  let carry = 0;

  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[j]!;
    const b = polygon[i]!;
    const length = Math.hypot(b.e - a.e, b.n - a.n);
    if (length === 0) continue;

    for (let d = carry; d < length; d += spacing) {
      const t = d / length;
      out.push({ e: a.e + (b.e - a.e) * t, n: a.n + (b.n - a.n) * t });
    }
    // Carry the remainder into the next edge so spacing does not reset at every corner.
    carry = (carry - length) % spacing;
    if (carry < 0) carry += spacing;
  }

  return out;
}

/**
 * Ribbon geometry along a ground centreline, for roads.
 *
 * Built as a triangle strip with mitred-enough joints: each vertex is offset along the
 * average of its adjacent segment normals, which is adequate for the shallow bends a site
 * road actually has and avoids the cost of a proper mitre solve.
 */
export function ribbon(centreline: Ground[], width: number, u = 0): BufferGeometry {
  const half = width / 2;
  const positions: number[] = [];
  const indices: number[] = [];
  const uvs: number[] = [];

  const normals: { e: number; n: number }[] = [];
  for (let i = 0; i < centreline.length; i++) {
    const prev = centreline[Math.max(i - 1, 0)]!;
    const next = centreline[Math.min(i + 1, centreline.length - 1)]!;
    const de = next.e - prev.e;
    const dn = next.n - prev.n;
    const len = Math.hypot(de, dn) || 1;
    // Left-hand normal of the direction of travel.
    normals.push({ e: -dn / len, n: de / len });
  }

  let travelled = 0;
  for (let i = 0; i < centreline.length; i++) {
    const p = centreline[i]!;
    const nrm = normals[i]!;
    if (i > 0) {
      const q = centreline[i - 1]!;
      travelled += Math.hypot(p.e - q.e, p.n - q.n);
    }

    positions.push(p.e + nrm.e * half, u, -(p.n + nrm.n * half));
    positions.push(p.e - nrm.e * half, u, -(p.n - nrm.n * half));
    uvs.push(0, travelled / width, 1, travelled / width);

    if (i > 0) {
      const base = (i - 1) * 2;
      indices.push(base, base + 1, base + 2, base + 1, base + 3, base + 2);
    }
  }

  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new Float32BufferAttribute(positions, 3));
  geometry.setAttribute('uv', new Float32BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return geometry;
}
