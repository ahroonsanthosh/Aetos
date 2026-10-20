'use client';

/**
 * Reusable physical primitives.
 *
 * These are the pieces that make infrastructure recognisable: lattice steel, fence runs,
 * insulator stacks, bushings. Everything repeated is instanced — a lattice mast is one
 * draw call for forty braces, not forty draw calls, which is what allows the site to carry
 * real structural detail without the frame budget collapsing.
 */

import { useMemo } from 'react';
import {
  BoxGeometry,
  CylinderGeometry,
  DoubleSide,
  Euler,
  InstancedMesh,
  Matrix4,
  Quaternion,
  Vector3,
  type Material,
} from 'three';

import type { Ground } from '@/types';
import { concreteMaterial, insulatorMaterial, steelDarkMaterial, steelMaterial } from '../materials/structure';

interface Transform {
  position: Vector3;
  rotation?: Euler;
  scale?: Vector3;
}

/** Build an InstancedMesh imperatively — cheaper and clearer than a ref-and-effect dance. */
function instanced(
  geometry: BoxGeometry | CylinderGeometry,
  material: Material,
  transforms: Transform[],
): InstancedMesh {
  const mesh = new InstancedMesh(geometry, material, transforms.length);
  const matrix = new Matrix4();
  const quaternion = new Quaternion();
  const scale = new Vector3(1, 1, 1);

  transforms.forEach((t, i) => {
    quaternion.setFromEuler(t.rotation ?? new Euler());
    matrix.compose(t.position, quaternion, t.scale ?? scale);
    mesh.setMatrixAt(i, matrix);
  });

  mesh.instanceMatrix.needsUpdate = true;
  mesh.castShadow = false;
  mesh.receiveShadow = false;
  return mesh;
}

// ---------------------------------------------------------------------------

interface LatticeProps {
  height: number;
  /** Width of the square section at the base. */
  base: number;
  /** Width at the top. Tapering reads as real structural steel. */
  top?: number;
  /** Vertical spacing between brace bays, metres. */
  bay?: number;
  legRadius?: number;
}

/**
 * A tapered lattice mast: four legs, horizontal rungs, and crossed diagonals per bay.
 *
 * Used for sensor masts and substation gantries. The taper and the diagonals are what
 * separate "lattice tower" from "four sticks" at a glance.
 */
export function Lattice({ height, base, top = base * 0.55, bay = 4, legRadius = 0.16 }: LatticeProps) {
  const { legs, braces } = useMemo(() => {
    const bays = Math.max(1, Math.round(height / bay));
    const bayHeight = height / bays;

    // Legs are thin boxes rather than cylinders: at this scale the silhouette is identical
    // and the geometry is a quarter of the vertices.
    const legTransforms: { position: Vector3; rotation?: Euler; scale?: Vector3 }[] = [];
    const corners = [
      [-1, -1],
      [1, -1],
      [1, 1],
      [-1, 1],
    ] as const;

    for (const [sx, sz] of corners) {
      const halfBase = base / 2;
      const halfTop = top / 2;
      const dx = (sx * halfTop - sx * halfBase) / height;
      const dz = (sz * halfTop - sz * halfBase) / height;
      const lean = Math.hypot(dx, dz);
      const length = height * Math.sqrt(1 + lean * lean);

      legTransforms.push({
        position: new Vector3((sx * (halfBase + halfTop)) / 2, height / 2, (sz * (halfBase + halfTop)) / 2),
        rotation: new Euler(Math.atan2(dz, 1) * -1, 0, Math.atan2(dx, 1)),
        scale: new Vector3(1, length / height, 1),
      });
    }

    const braceTransforms: { position: Vector3; rotation?: Euler; scale?: Vector3 }[] = [];
    for (let i = 0; i < bays; i++) {
      const y0 = i * bayHeight;
      const y1 = y0 + bayHeight;
      const w0 = base + ((top - base) * y0) / height;
      const w1 = base + ((top - base) * y1) / height;
      const wMid = (w0 + w1) / 2;

      // Horizontal rungs at the top of each bay.
      for (const axis of [0, 1]) {
        for (const side of [-1, 1]) {
          braceTransforms.push({
            position: new Vector3(
              axis === 0 ? 0 : (side * w1) / 2,
              y1,
              axis === 0 ? (side * w1) / 2 : 0,
            ),
            rotation: new Euler(0, axis === 0 ? 0 : Math.PI / 2, Math.PI / 2),
            scale: new Vector3(1, w1, 1),
          });
        }
      }

      // Crossed diagonals on each face.
      const diagonal = Math.hypot(wMid, bayHeight);
      const tilt = Math.atan2(wMid, bayHeight);
      for (const axis of [0, 1]) {
        for (const side of [-1, 1]) {
          for (const dir of [-1, 1]) {
            braceTransforms.push({
              position: new Vector3(
                axis === 0 ? 0 : (side * wMid) / 2,
                y0 + bayHeight / 2,
                axis === 0 ? (side * wMid) / 2 : 0,
              ),
              rotation:
                axis === 0
                  ? new Euler(0, 0, dir * tilt)
                  : new Euler(dir * tilt, Math.PI / 2, 0),
              scale: new Vector3(1, diagonal, 1),
            });
          }
        }
      }
    }

    const legGeometry = new BoxGeometry(legRadius * 2, height, legRadius * 2);
    const braceGeometry = new BoxGeometry(legRadius * 1.1, 1, legRadius * 1.1);

    return {
      legs: instanced(legGeometry, steelMaterial, legTransforms),
      braces: instanced(braceGeometry, steelDarkMaterial, braceTransforms),
    };
  }, [height, base, top, bay, legRadius]);

  return (
    <group>
      <primitive object={legs} />
      <primitive object={braces} />
    </group>
  );
}

// ---------------------------------------------------------------------------

interface FenceRunProps {
  /** Closed or open run of ground points, local to the parent group. */
  points: Ground[];
  height?: number;
  postSpacing?: number;
  closed?: boolean;
}

/**
 * Security fencing: posts plus a translucent mesh panel.
 *
 * The panel is a thin double-sided plane rather than modelled mesh — at any camera
 * distance the operator will ever use, a semi-transparent plane and a million-poly chain
 * link are indistinguishable, and one of them costs nothing.
 */
export function FenceRun({ points, height = 3, postSpacing = 12, closed = true }: FenceRunProps) {
  const { posts, panels } = useMemo(() => {
    const postTransforms: Transform[] = [];
    const panelTransforms: Transform[] = [];

    const limit = closed ? points.length : points.length - 1;
    for (let i = 0; i < limit; i++) {
      const a = points[i]!;
      const b = points[(i + 1) % points.length]!;

      const de = b.e - a.e;
      const dn = b.n - a.n;
      const length = Math.hypot(de, dn);
      if (length === 0) continue;

      // The panel is a unit plane scaled along its width, so one geometry serves every run.
      panelTransforms.push({
        position: new Vector3((a.e + b.e) / 2, height / 2, -(a.n + b.n) / 2),
        rotation: new Euler(0, -Math.atan2(de, dn), 0),
        scale: new Vector3(length, 1, 1),
      });

      const count = Math.max(1, Math.floor(length / postSpacing));
      for (let p = 0; p < count; p++) {
        const t = p / count;
        postTransforms.push({
          position: new Vector3(a.e + de * t, height / 2, -(a.n + dn * t)),
        });
      }
    }

    return {
      posts: instanced(new BoxGeometry(0.18, height, 0.18), steelMaterial, postTransforms),
      panels: panelTransforms,
    };
  }, [points, height, postSpacing, closed]);

  return (
    <group>
      <primitive object={posts} />
      {panels.map((p, i) => (
        <mesh key={i} position={p.position} rotation={p.rotation} scale={p.scale}>
          <planeGeometry args={[1, height]} />
          <meshStandardMaterial
            color="#1a2830"
            transparent
            opacity={0.16}
            side={DoubleSide}
            roughness={0.85}
            metalness={0.1}
            depthWrite={false}
          />
        </mesh>
      ))}
    </group>
  );
}

// ---------------------------------------------------------------------------

/**
 * A stack of ceramic discs on a steel post. The single most recognisable substation cue
 * after the lattice gantry itself.
 */
export function InsulatorStack({
  height = 2.4,
  discs = 6,
  radius = 0.34,
}: {
  height?: number;
  discs?: number;
  radius?: number;
}) {
  const mesh = useMemo(() => {
    const transforms = Array.from({ length: discs }, (_, i) => ({
      position: new Vector3(0, ((i + 0.5) * height) / discs, 0),
    }));
    return instanced(new CylinderGeometry(radius, radius, height / discs / 2.2, 8), insulatorMaterial, transforms);
  }, [height, discs, radius]);

  return (
    <group>
      <mesh position={[0, height / 2, 0]}>
        <cylinderGeometry args={[radius * 0.32, radius * 0.32, height, 6]} />
        <primitive object={steelMaterial} attach="material" />
      </mesh>
      <primitive object={mesh} />
    </group>
  );
}

/** Transformer bushing: tapered porcelain with a terminal cap. */
export function Bushing({ height = 2.6, radius = 0.3 }: { height?: number; radius?: number }) {
  return (
    <group>
      <mesh position={[0, height / 2, 0]}>
        <cylinderGeometry args={[radius * 0.55, radius, height, 10]} />
        <primitive object={insulatorMaterial} attach="material" />
      </mesh>
      <mesh position={[0, height + 0.12, 0]}>
        <cylinderGeometry args={[radius * 0.7, radius * 0.7, 0.24, 10]} />
        <primitive object={steelMaterial} attach="material" />
      </mesh>
    </group>
  );
}

/** Concrete pad. Grounds a structure so it does not look like it is hovering. */
export function Pad({
  width,
  depth,
  height = 0.35,
}: {
  width: number;
  depth: number;
  height?: number;
}) {
  return (
    <mesh position={[0, height / 2, 0]}>
      <boxGeometry args={[width, height, depth]} />
      <primitive object={concreteMaterial} attach="material" />
    </mesh>
  );
}
