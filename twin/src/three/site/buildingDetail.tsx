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

import type { FacadeDesign, Ground } from '@/types';
import { polygonBounds, polygonCentroid, pointInPolygon } from '@/lib/geometry';
import { yawFor } from '../frame';
import { extrudedPolygon, nearestCompassDirection, polygonEdges, sampleFacade } from '../lib/extrude';
import {
  EDGE_COLOUR,
  concreteMaterial,
  doorMaterial,
  enclosureMaterial,
  glazingMaterial,
} from '../materials/structure';

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
// A generic 1×1×1 box, scaled per instance — shared by roof units (random sizes) and photo
// facade openings (whatever size the photo said), neither of which can share fixed-size
// geometry the way the procedural window band does.
const unitBoxGeometry = new BoxGeometry(1, 1, 1);

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

    const instanced = new InstancedMesh(unitBoxGeometry, enclosureMaterial, positions.length);
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
 * An opening's world transform on one wall edge: centred at `xFrac` (+ half its width)
 * along the edge and `yFrac` (+ half its height) up the building, sitting just proud of
 * the wall face along its outward normal — the same placement rule `FacadeWindows` uses
 * for a procedural row, just driven by a photo's fractions instead of even spacing.
 */
function openingMatrix(
  edge: { a: Ground; b: Ground; length: number; normal: { e: number; n: number } },
  height: number,
  opening: { x: number; y: number; width: number; height: number },
  clearance: number,
  out: Matrix4,
): void {
  const centreFrac = opening.x + opening.width / 2;
  const e = edge.a.e + (edge.b.e - edge.a.e) * centreFrac + edge.normal.e * clearance;
  const n = edge.a.n + (edge.b.n - edge.a.n) * centreFrac + edge.normal.n * clearance;
  const y = (opening.y + opening.height / 2) * height;

  const position = new Vector3(e, y, -n);
  const euler = new Euler(0, yawFor(Math.atan2(edge.normal.e, edge.normal.n)), 0);
  const quaternion = new Quaternion().setFromEuler(euler);
  const scale = new Vector3(Math.max(opening.width * edge.length, 0.1), Math.max(opening.height * height, 0.1), 1);
  out.compose(position, quaternion, scale);
}

/**
 * Windows and doors transcribed from real photographs — see `site/photoFacadePrompt.ts`
 * for how the operator produces the JSON this reads. Each wall of the traced or imported
 * footprint is matched to the compass side it most nearly faces; a side with no entry in
 * `facade.walls` (not photographed, or the transcriber was not confident about it) is left
 * blank rather than filled in with a guess.
 *
 * Unit geometry (a 1×1×1 box) is scaled per instance rather than shared like the
 * procedural windows, since every opening here can be a different size — that is the
 * entire point of reading it from a photo instead of a formula.
 */
function PhotoFacade({
  outline,
  height,
  facade,
}: {
  outline: Ground[];
  height: number;
  facade: FacadeDesign;
}) {
  const { windows, doors } = useMemo(() => {
    const windowMatrices: Matrix4[] = [];
    const doorMatrices: Matrix4[] = [];
    const scratch = new Matrix4();

    for (const edge of polygonEdges(outline)) {
      const wall = facade.walls[nearestCompassDirection(edge.normal)];
      if (!wall) continue;

      for (const opening of wall.windows) {
        openingMatrix(edge, height, opening, WINDOW_DEPTH / 2 + 0.02, scratch);
        windowMatrices.push(scratch.clone());
      }
      for (const opening of wall.doors) {
        openingMatrix(edge, height, opening, 0.07, scratch);
        doorMatrices.push(scratch.clone());
      }
    }

    const build = (matrices: Matrix4[], material: typeof glazingMaterial) => {
      if (matrices.length === 0) return null;
      const instanced = new InstancedMesh(unitBoxGeometry, material, matrices.length);
      matrices.forEach((m, i) => instanced.setMatrixAt(i, m));
      instanced.instanceMatrix.needsUpdate = true;
      instanced.frustumCulled = false;
      return instanced;
    };

    return {
      windows: build(windowMatrices, glazingMaterial),
      doors: build(doorMatrices, doorMaterial),
    };
  }, [outline, height, facade]);

  return (
    <>
      {windows && <primitive object={windows} />}
      {doors && <primitive object={doors} />}
    </>
  );
}

/**
 * Entry point. Two entirely separate paths, not a blend: a building with a photo facade
 * shows exactly what was transcribed and nothing else — no procedural windows filling in
 * gaps, no parapet, no invented roof plant — because the whole reason it exists is to stop
 * short of anything that was not actually in the photographs. Everything else keeps the
 * automatic massing detail, gated behind `qualifiesForDetail` as before.
 */
export function BuildingDetail({
  outline,
  height,
  seed,
  facade,
}: {
  outline: Ground[];
  height: number;
  seed: string;
  facade?: FacadeDesign;
}) {
  if (facade) return <PhotoFacade outline={outline} height={height} facade={facade} />;

  if (!qualifiesForDetail(outline, height)) return null;

  return (
    <>
      <FacadeWindows outline={outline} height={height} />
      <RoofParapet outline={outline} height={height} />
      <RoofUnits outline={outline} height={height} seed={seed} />
    </>
  );
}

