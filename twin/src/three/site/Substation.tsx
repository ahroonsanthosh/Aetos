'use client';

/**
 * The substation.
 *
 * The brief's test case: "a substation should actually look like a substation". So this is
 * modelled from the features that actually identify one — lattice gantries carrying
 * busbars, insulator stacks, circuit breakers in a row, a fenced compound on a concrete
 * pad — rather than reduced to a glowing box with a label.
 *
 * It is also the most critical asset on the site, so it is the structure the operator will
 * look at most, and the one whose protected volume they will judge the whole interface by.
 */

import { useMemo } from 'react';
import { Edges } from '@react-three/drei';

import type { Asset, Ground } from '@/types';
import { EDGE_COLOUR, enclosureMaterial, steelMaterial } from '../materials/structure';
import { Bushing, FenceRun, InsulatorStack, Lattice, Pad } from './parts';
import { SelectableAsset } from './SelectableAsset';

const COMPOUND_W = 110;
const COMPOUND_D = 84;

/** Gantry bays, positioned along the compound's north-south axis. */
const BAYS = [-24, 0, 24];
const GANTRY_SPAN = 34;
const GANTRY_HEIGHT = 13;

export function Substation({ asset }: { asset: Asset }) {
  // Fence points are in the parent group's local ground frame (e, n), which
  // `SelectableAsset` has already positioned and rotated.
  const fence = useMemo<Ground[]>(
    () => [
      { e: -COMPOUND_W / 2, n: -COMPOUND_D / 2 },
      { e: COMPOUND_W / 2, n: -COMPOUND_D / 2 },
      { e: COMPOUND_W / 2, n: COMPOUND_D / 2 },
      { e: -COMPOUND_W / 2, n: COMPOUND_D / 2 },
    ],
    [],
  );

  return (
    <SelectableAsset asset={asset} ringRadius={72}>
      <Pad width={COMPOUND_W} depth={COMPOUND_D} height={0.3} />
      <FenceRun points={fence} height={3.2} postSpacing={9} />

      {/* Gantry bays: paired lattice masts carrying a cross beam and busbars. */}
      {BAYS.map((z) => (
        <group key={z} position={[0, 0, z]}>
          <group position={[-GANTRY_SPAN / 2, 0.3, 0]}>
            <Lattice height={GANTRY_HEIGHT} base={2.6} top={1.5} bay={3.2} />
          </group>
          <group position={[GANTRY_SPAN / 2, 0.3, 0]}>
            <Lattice height={GANTRY_HEIGHT} base={2.6} top={1.5} bay={3.2} />
          </group>

          {/* Cross beam. */}
          <mesh position={[0, GANTRY_HEIGHT + 0.3, 0]}>
            <boxGeometry args={[GANTRY_SPAN + 2.2, 0.7, 0.9]} />
            <primitive object={steelMaterial} attach="material" />
            <Edges threshold={20} color={EDGE_COLOUR} />
          </mesh>

          {/* Busbars slung beneath the beam — three phases, visibly separated. */}
          {[-1, 0, 1].map((phase) => (
            <mesh
              key={phase}
              position={[0, GANTRY_HEIGHT - 1.6, phase * 1.5]}
              rotation={[0, 0, Math.PI / 2]}
            >
              <cylinderGeometry args={[0.13, 0.13, GANTRY_SPAN, 6]} />
              <primitive object={steelMaterial} attach="material" />
            </mesh>
          ))}

          {/* Insulator stacks supporting each phase at the bay ends. */}
          {[-1, 1].map((side) =>
            [-1, 0, 1].map((phase) => (
              <group
                key={`${side}-${phase}`}
                position={[(side * GANTRY_SPAN) / 2.6, 0.3, phase * 1.5]}
              >
                <InsulatorStack height={3.4} discs={7} radius={0.3} />
              </group>
            )),
          )}
        </group>
      ))}

      {/* Circuit breakers: a disciplined row is a strong substation cue. */}
      {[-2, -1, 0, 1, 2].map((i) => (
        <group key={i} position={[i * 9, 0.3, -COMPOUND_D / 2 + 14]}>
          <mesh position={[0, 1.6, 0]}>
            <boxGeometry args={[2.4, 3.2, 2.2]} />
            <primitive object={enclosureMaterial} attach="material" />
            <Edges threshold={20} color={EDGE_COLOUR} />
          </mesh>
          {[-0.7, 0.7].map((z) => (
            <group key={z} position={[0, 3.2, z]}>
              <Bushing height={2.8} radius={0.26} />
            </group>
          ))}
        </group>
      ))}

      {/* Relay and control kiosk. */}
      <group position={[-COMPOUND_W / 2 + 12, 0.3, COMPOUND_D / 2 - 10]}>
        <mesh position={[0, 1.7, 0]}>
          <boxGeometry args={[12, 3.4, 6]} />
          <primitive object={enclosureMaterial} attach="material" />
          <Edges threshold={20} color={EDGE_COLOUR} />
        </mesh>
      </group>
    </SelectableAsset>
  );
}
