'use client';

/**
 * The coverage map — "where am I vulnerable?" as a picture.
 *
 * Drawn as a single instanced grid lying just above the terrain, coloured green / amber /
 * red by detection confidence at 60 m. Instanced with per-instance colour, so the whole
 * map is one draw call however fine the grid gets.
 *
 * It is deliberately laid *into* the environment rather than shown as a separate chart.
 * The point of the feature is that an operator can look at the site and see the hole, in
 * the place where the hole is.
 */

import { useMemo } from 'react';
import {
  AdditiveBlending,
  BoxGeometry,
  Color,
  DoubleSide,
  InstancedMesh,
  Matrix4,
  MeshBasicMaterial,
  Vector3,
} from 'three';

import { coverageColour } from '@/lib/palette';
import { useAetos } from '@/state/store';

export function CoverageMap() {
  const visible = useAetos((s) => s.layers.coverageMap);
  const grid = useAetos((s) => s.snapshot?.coverage ?? null);

  const mesh = useMemo(() => {
    if (!grid) return null;

    const size = grid.resolution * 0.86;
    const geometry = new BoxGeometry(size, 0.01, size);
    // Additive, so a cell can only ever add light to the ground beneath it. Drawn with
    // normal blending, a low-weighted "strong" cell is darker than the terrain it sits on,
    // and a map of good coverage renders as a black checkerboard — the exact opposite of
    // what it is reporting.
    const material = new MeshBasicMaterial({
      transparent: true,
      opacity: 0.55,
      depthWrite: false,
      side: DoubleSide,
      vertexColors: true,
      blending: AdditiveBlending,
    });

    const instanced = new InstancedMesh(geometry, material, grid.cells.length);
    const matrix = new Matrix4();
    const position = new Vector3();
    const colour = new Color();

    grid.cells.forEach((cell, i) => {
      position.set(cell.e, 0.3, -cell.n);
      matrix.makeTranslation(position.x, position.y, position.z);
      instanced.setMatrixAt(i, matrix);

      colour.set(coverageColour[cell.quality]);
      // Strong coverage is deliberately understated and gaps are not: the operator needs
      // to find the weakness, not admire the coverage.
      const weight = cell.quality === 'strong' ? 0.16 : cell.quality === 'reduced' ? 0.55 : 1;
      colour.multiplyScalar(weight);
      instanced.setColorAt(i, colour);
    });

    instanced.instanceMatrix.needsUpdate = true;
    if (instanced.instanceColor) instanced.instanceColor.needsUpdate = true;
    instanced.frustumCulled = false;

    return instanced;
  }, [grid]);

  if (!visible || !mesh) return null;

  return <primitive object={mesh} />;
}
