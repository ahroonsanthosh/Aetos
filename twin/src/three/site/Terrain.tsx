'use client';

/**
 * Ground, roads and site boundary.
 *
 * The ground is the thing that stops the site looking like objects floating in space. It
 * carries the reference grid, and it fades out at range so the scene has a horizon rather
 * than an edge.
 */

import { useMemo } from 'react';
import { AdditiveBlending, DoubleSide, Shape, ShapeGeometry } from 'three';
import { Line } from '@react-three/drei';

import type { Ground, Site } from '@/types';
import { palette } from '@/lib/palette';
import { createGroundMaterial } from '../materials/ground';
import { roadMaterial } from '../materials/structure';
import { ribbon } from '../lib/extrude';
import { FenceRun } from './parts';

/** Half-extent of the ground plane. Well beyond the site so tracks have somewhere to be. */
const GROUND_EXTENT = 2600;

export function Terrain({ site }: { site: Site }) {
  const groundMaterial = useMemo(
    () =>
      createGroundMaterial({
        ground: palette.ground,
        grid: palette.grid,
        gridMajor: palette.gridMajor,
        minor: 25,
        major: 250,
        fadeStart: 800,
        fadeEnd: 2400,
      }),
    [],
  );

  const roads = useMemo(
    () => site.roads.map((centreline) => ribbon(centreline, 7, 0.06)),
    [site.roads],
  );

  const boundaryLine = useMemo(
    () => closedLoop(site.boundary, 0.2),
    [site.boundary],
  );

  return (
    <group>
      {/* Ground plane. Built in XY and laid flat, which is what the grid shader expects. */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0, 0]}>
        <planeGeometry args={[GROUND_EXTENT * 2, GROUND_EXTENT * 2, 1, 1]} />
        <primitive object={groundMaterial} attach="material" />
      </mesh>

      {/* Roads. */}
      {roads.map((geometry, i) => (
        <mesh key={i} geometry={geometry}>
          <primitive object={roadMaterial} attach="material" />
        </mesh>
      ))}

      {/* Site boundary: a drawn line plus real fencing, so the perimeter reads as both a
          physical barrier and a piece of the system's model of the site. */}
      <Line points={boundaryLine} color={palette.holoDim} lineWidth={1.4} transparent opacity={0.55} />
      <FenceRun points={site.boundary} height={2.8} postSpacing={18} />

      {/* A faint filled footprint, so the protected area reads as a place at a glance. */}
      <SiteFootprint boundary={site.boundary} />
    </group>
  );
}

/** Closed loop of world-space points at a given altitude. */
function closedLoop(polygon: Ground[], u: number): [number, number, number][] {
  const points = polygon.map((p) => [p.e, u, -p.n] as [number, number, number]);
  const first = points[0];
  if (first) points.push(first);
  return points;
}

/**
 * The site footprint as a faint additive fill. Deliberately very low opacity — it should
 * register peripherally as "the ground inside the fence is different" and never compete
 * with anything the operator is actually reading.
 */
function SiteFootprint({ boundary }: { boundary: Ground[] }) {
  const geometry = useMemo(() => {
    const shape = new Shape();
    const first = boundary[0]!;
    shape.moveTo(first.e, first.n);
    for (let i = 1; i < boundary.length; i++) {
      const p = boundary[i]!;
      shape.lineTo(p.e, p.n);
    }
    shape.closePath();
    const g = new ShapeGeometry(shape);
    g.rotateX(-Math.PI / 2);
    return g;
  }, [boundary]);

  return (
    <mesh geometry={geometry} position={[0, 0.05, 0]}>
      <meshBasicMaterial
        color={palette.holoDeep}
        transparent
        opacity={0.16}
        blending={AdditiveBlending}
        depthWrite={false}
        side={DoubleSide}
      />
    </mesh>
  );
}
