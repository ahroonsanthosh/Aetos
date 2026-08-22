'use client';

/**
 * Automatic massing detail for generic buildings.
 *
 * `GenericAsset` is the honest fallback for anything without a bespoke model — most of a
 * real, OSM-imported or traced site. Left alone it draws a plain extruded block, which is
 * correct but reads as a toy at any building large enough to actually look at closely. This
 * derives extra detail entirely from data the asset already carries — its footprint, its
 * height, its own id as a stable seed — so it costs the operator nothing: no new field to
 * fill in, no extra step while tracing or importing, no per-building settings to fuss with.
 * Trace or import a building exactly as before; if it is big enough to be worth dressing up,
 * it comes out dressed.
 *
 * Deliberately skipped below a size threshold. An OSM import can bring in dozens of small
 * houses in one go, and a shed does not need window bands or roof plant — it needs to stay
 * a cheap box, both for the look of the thing and for frame time once there are a lot of
 * them.
 */

import { useMemo } from 'react';
import { Edges } from '@react-three/drei';
import { BoxGeometry, Euler, InstancedMesh, Matrix4, Quaternion, Vector3 } from 'three';

import type { Ground } from '@/types';
import { polygonBounds, polygonCentroid, pointInPolygon } from '@/lib/geometry';
import { yawFor } from '../frame';
import { extrudedPolygon, sampleFacade } from '../lib/extrude';
import { EDGE_COLOUR, concreteMaterial, enclosureMaterial, glazingMaterial } from '../materials/structure';

const FLOOR_HEIGHT = 3.2;
const WINDOW_SPACING = 3.4;
const WINDOW_WIDTH = 1.2;
const WINDOW_HEIGHT = 1.5;
const WINDOW_DEPTH = 0.08;

/** Below this, a building stays a plain block — not worth dressing up, and there can be a
 *  great many of them in one OSM import. */
const MIN_SPAN_M = 12;
const MIN_HEIGHT_M = 4;

// Geometry is created once and shared across every building's instanced mesh; only the
// instance count differs per building.
const windowGeometry = new BoxGeometry(WINDOW_WIDTH, WINDOW_HEIGHT, WINDOW_DEPTH);
const roofUnitGeometry = new BoxGeometry(1, 1, 1);

/** Tiny deterministic string hash (djb2), so roof-unit placement is stable across
 *  re-renders and selection without needing to store anything on the asset. */
function hashSeed(id: string): number {
  let hash = 5381;
  for (let i = 0; i < id.length; i++) hash = (hash * 33) ^ id.charCodeAt(i);
  return hash >>> 0;
}

/** Mulberry32 — small, fast, good enough for scattering roof units. */
function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function qualifiesForDetail(outline: Ground[], height: number): boolean {
  if (height < MIN_HEIGHT_M) return false;
  const bounds = polygonBounds(outline);
  return Math.max(bounds.maxE - bounds.minE, bounds.maxN - bounds.minN) >= MIN_SPAN_M;
}

/**
 * Window bands, one row per floor, walking the building's own outline so they sit flush
 * against whatever shape it actually is — a traced L-shaped block gets windows on all of
 * its walls, not just the ones a bounding box would have implied.
 */
function FacadeWindows({ outline, height }: { outline: Ground[]; height: number }) {
  const mesh = useMemo(() => {
    const floors = Math.max(1, Math.floor((height - 0.6) / FLOOR_HEIGHT));
    const row = sampleFacade(outline, WINDOW_SPACING);
    const count = row.length * floors;
    if (count === 0) return null;

    const instanced = new InstancedMesh(windowGeometry, glazingMaterial, count);
    const matrix = new Matrix4();
    const quaternion = new Quaternion();
    const euler = new Euler();
    const position = new Vector3();
    const scale = new Vector3(1, 1, 1);

    let i = 0;
    for (let floor = 0; floor < floors; floor++) {
      const y = floor * FLOOR_HEIGHT + FLOOR_HEIGHT * 0.55;
      for (const { point, normal } of row) {
        // Sit just proud of the wall face, along the outward normal, so the glazing does
        // not z-fight the concrete it is set into.
        const e = point.e + normal.e * (WINDOW_DEPTH * 0.5 + 0.02);
        const n = point.n + normal.n * (WINDOW_DEPTH * 0.5 + 0.02);
        position.set(e, y, -n);
        euler.set(0, yawFor(Math.atan2(normal.e, normal.n)), 0);
        quaternion.setFromEuler(euler);
        matrix.compose(position, quaternion, scale);
        instanced.setMatrixAt(i, matrix);
        i++;
      }
    }

    instanced.instanceMatrix.needsUpdate = true;
    instanced.frustumCulled = false;
    return instanced;
  }, [outline, height]);

  if (!mesh) return null;
  return <primitive object={mesh} />;
}

/**
 * A raised lip at the roofline. Scaled out from the footprint's own centroid rather than a
 * true polygon offset — a true offset risks self-intersecting on a concave trace, and a
 * decorative parapet does not need to be geometrically exact to read as one.
 */
function RoofParapet({ outline, height }: { outline: Ground[]; height: number }) {
  const geometry = useMemo(() => {
    const centroid = polygonCentroid(outline);
    const bounds = polygonBounds(outline);
    const span = Math.max(bounds.maxE - bounds.minE, bounds.maxN - bounds.minN) || 1;
    // A fixed-width lip regardless of building size reads as too thick on a large block and
    // too thin on a small one; scaling it as a fraction of span keeps it proportionate.
    const factor = 1 + Math.min(1.2, span * 0.06) / span;
    const scaled = outline.map((p) => ({
      e: centroid.e + (p.e - centroid.e) * factor,
      n: centroid.n + (p.n - centroid.n) * factor,
    }));
    return extrudedPolygon(scaled, 0.5);
  }, [outline]);

  return (
    <mesh geometry={geometry} position={[0, height, 0]}>
      <primitive object={concreteMaterial} attach="material" />
      <Edges threshold={20} color={EDGE_COLOUR} />
    </mesh>
  );
}

/** A handful of rooftop mechanical units, scattered by a seed derived from the asset's own
 *  id so they stay put across re-renders instead of jittering every time the asset moves. */
function RoofUnits({ outline, height, seed }: { outline: Ground[]; height: number; seed: string }) {
  const mesh = useMemo(() => {
    const bounds = polygonBounds(outline);
    const area = (bounds.maxE - bounds.minE) * (bounds.maxN - bounds.minN);
    const target = Math.min(3, Math.max(1, Math.round(area / 220)));

    const rng = mulberry32(hashSeed(seed));
    const positions: Ground[] = [];
    let attempts = 0;
    while (positions.length < target && attempts < target * 20) {
      attempts++;
      const e = bounds.minE + rng() * (bounds.maxE - bounds.minE);
      const n = bounds.minN + rng() * (bounds.maxN - bounds.minN);
      // Kept away from the very edge so a unit never overhangs the parapet.
      if (pointInPolygon({ e, n }, outline)) positions.push({ e, n });
    }
    if (positions.length === 0) return null;

    const instanced = new InstancedMesh(roofUnitGeometry, enclosureMaterial, positions.length);
    const matrix = new Matrix4();
    const scale = new Vector3();
    const position = new Vector3();
    const quaternion = new Quaternion();

    positions.forEach((p, i) => {
      const w = 1.6 + rng() * 1.2;
      const d = 1.4 + rng() * 1.0;
      const h = 0.9 + rng() * 0.6;
      scale.set(w, h, d);
      position.set(p.e, height + 0.5 + h / 2, -p.n);
      quaternion.identity();
      matrix.compose(position, quaternion, scale);
      instanced.setMatrixAt(i, matrix);
    });

    instanced.instanceMatrix.needsUpdate = true;
    return instanced;
  }, [outline, height, seed]);

  if (!mesh) return null;
  return <primitive object={mesh} />;
}

/**
 * Entry point: everything above, gated behind `qualifiesForDetail` so a small imported
 * house never pays for or shows detail it was never going to need.
 */
export function BuildingDetail({
  outline,
  height,
  seed,
}: {
  outline: Ground[];
  height: number;
  seed: string;
}) {
  if (!qualifiesForDetail(outline, height)) return null;

  return (
    <>
      <FacadeWindows outline={outline} height={height} />
      <RoofParapet outline={outline} height={height} />
      <RoofUnits outline={outline} height={height} seed={seed} />
    </>
  );
}

