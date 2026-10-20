'use client';

/**
 * The solar field.
 *
 * Six blocks, twelve rows each, thirty tables per row — 2,160 tables. Drawn as three
 * instanced meshes (panel, torque tube, piles) rather than 2,160 objects, which is the
 * difference between a scene that holds 60 fps and one that does not.
 *
 * Selection works through `instanceId`: the click lands on the instanced mesh, and a
 * lookup maps the instance back to the array asset it belongs to. That keeps instancing
 * without giving up the ability to click a panel and have the system tell you what it is.
 */

import { useMemo } from 'react';
import { useFrame, type ThreeEvent } from '@react-three/fiber';
import {
  BoxGeometry,
  CylinderGeometry,
  Euler,
  InstancedMesh,
  Matrix4,
  Quaternion,
  Vector3,
} from 'three';

import type { SolarArrayBlock } from '@/types';
import { panelMaterial, steelDarkMaterial, steelMaterial } from '../materials/structure';
import { useAetos } from '@/state/store';

/** Panel table geometry, metres. */
const TABLE_WIDTH = 4.4;
const TABLE_DEPTH = 2.5;
const TILT = (24 * Math.PI) / 180;
const TUBE_HEIGHT = 1.9;

export function SolarField({ blocks }: { blocks: SolarArrayBlock[] }) {
  const select = useAetos((s) => s.select);
  const mode = useAetos((s) => s.mode);

  const { panels, tubes, piles, ownerByInstance } = useMemo(() => {
    const matrices: Matrix4[] = [];
    const owners: string[] = [];

    const matrix = new Matrix4();
    const quaternion = new Quaternion();
    const position = new Vector3();
    const scale = new Vector3(1, 1, 1);
    const euler = new Euler();

    for (const block of blocks) {
      for (let row = 0; row < block.rows; row++) {
        for (let col = 0; col < block.tablesPerRow; col++) {
          const e = block.origin.e + col * block.tablePitch + TABLE_WIDTH / 2;
          const n = block.origin.n + row * block.rowPitch;

          euler.set(0, block.heading, 0);
          position.set(e, TUBE_HEIGHT, -n);
          quaternion.setFromEuler(euler);
          matrix.compose(position, quaternion, scale);
          matrices.push(matrix.clone());
          owners.push(block.assetId);
        }
      }
    }

    const count = matrices.length;

    // Panel: tilted about the east-west axis so the face looks south, as it would in the
    // northern hemisphere. The tilt is baked into the geometry rather than each instance.
    const panelGeometry = new BoxGeometry(TABLE_WIDTH, 0.07, TABLE_DEPTH);
    panelGeometry.rotateX(TILT);
    const panelMesh = new InstancedMesh(panelGeometry, panelMaterial, count);

    const tubeGeometry = new CylinderGeometry(0.09, 0.09, TABLE_WIDTH, 6);
    tubeGeometry.rotateZ(Math.PI / 2);
    tubeGeometry.translate(0, -0.18, 0);
    const tubeMesh = new InstancedMesh(tubeGeometry, steelMaterial, count);

    // Two piles per table, so one instanced mesh at double the count.
    const pileGeometry = new BoxGeometry(0.13, TUBE_HEIGHT, 0.13);
    pileGeometry.translate(0, -TUBE_HEIGHT / 2 - 0.18, 0);
    const pileMesh = new InstancedMesh(pileGeometry, steelDarkMaterial, count * 2);

    const offset = new Matrix4();
    for (let i = 0; i < count; i++) {
      const m = matrices[i]!;
      panelMesh.setMatrixAt(i, m);
      tubeMesh.setMatrixAt(i, m);

      // premultiply(m) gives m * offset — the offset applied in the table's local frame.
      // It does not mutate m, so no clone is needed here.
      offset.makeTranslation(-TABLE_WIDTH * 0.32, 0, 0);
      pileMesh.setMatrixAt(i * 2, offset.premultiply(m));
      offset.makeTranslation(TABLE_WIDTH * 0.32, 0, 0);
      pileMesh.setMatrixAt(i * 2 + 1, offset.premultiply(m));
    }

    panelMesh.instanceMatrix.needsUpdate = true;
    tubeMesh.instanceMatrix.needsUpdate = true;
    pileMesh.instanceMatrix.needsUpdate = true;

    // Frustum culling on a single mesh spanning the whole field is worse than useless —
    // it can only ever cull all of it or none of it, and computing the bounds costs.
    panelMesh.frustumCulled = false;
    tubeMesh.frustumCulled = false;
    pileMesh.frustumCulled = false;

    return { panels: panelMesh, tubes: tubeMesh, piles: pileMesh, ownerByInstance: owners };
  }, [blocks]);

  /**
   * The array field is context, not a threat surface. In threat mode it drops back so the
   * tracks and protected volumes carry the picture.
   */
  useFrame((_, dt) => {
    const target = mode === 'threat' ? 0.3 : 1;
    const current = panelMaterial.emissiveIntensity;
    const next = current + (target * 0.55 - current) * Math.min(dt * 3, 1);
    panelMaterial.emissiveIntensity = next;
  });

  const onClick = (event: ThreeEvent<MouseEvent>) => {
    const id = event.instanceId;
    if (id === undefined) return;
    const assetId = ownerByInstance[id];
    if (!assetId) return;
    event.stopPropagation();
    select({ kind: 'asset', id: assetId });
  };

  return (
    <group>
      <primitive
        object={panels}
        onClick={onClick}
        onPointerOver={() => (document.body.style.cursor = 'pointer')}
        onPointerOut={() => (document.body.style.cursor = 'default')}
      />
      <primitive object={tubes} />
      <primitive object={piles} />
    </group>
  );
}
