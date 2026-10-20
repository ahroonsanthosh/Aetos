'use client';

/**
 * The remaining physical plant.
 *
 * Each structure is modelled from whatever makes its type readable at a glance: radiator
 * fins and bushings for a transformer, a stepped roof and door bay for the control
 * building, a run of containerised units for storage. None of them is a labelled cube.
 */

import { Edges } from '@react-three/drei';

import type { Asset } from '@/types';
import {
  EDGE_COLOUR,
  concreteMaterial,
  enclosureMaterial,
  steelMaterial,
} from '../materials/structure';
import { Bushing, Pad } from './parts';
import { SelectableAsset } from './SelectableAsset';

/**
 * Oil-filled power transformer: tank, radiator banks either side, HV bushings on top,
 * conservator drum. The radiators are the silhouette that identifies it.
 */
export function Transformer({ asset }: { asset: Asset }) {
  const { width, depth, height } = asset.footprint;

  return (
    <SelectableAsset asset={asset} ringRadius={18}>
      <Pad width={width + 6} depth={depth + 6} height={0.4} />

      {/* Tank. */}
      <mesh position={[0, 0.4 + height * 0.42, 0]}>
        <boxGeometry args={[width, height * 0.84, depth]} />
        <primitive object={enclosureMaterial} attach="material" />
        <Edges threshold={20} color={EDGE_COLOUR} />
      </mesh>

      {/* Radiator banks — thin fins, the most recognisable feature. */}
      {[-1, 1].map((side) => (
        <group key={side} position={[0, 0.4 + height * 0.42, (side * depth) / 2 + side * 0.9]}>
          {Array.from({ length: 9 }, (_, i) => (
            <mesh key={i} position={[(i - 4) * (width / 10), 0, 0]}>
              <boxGeometry args={[width / 22, height * 0.66, 1.6]} />
              <primitive object={steelMaterial} attach="material" />
            </mesh>
          ))}
        </group>
      ))}

      {/* Conservator drum. */}
      <mesh position={[0, 0.4 + height * 0.93, -depth * 0.2]} rotation={[0, 0, Math.PI / 2]}>
        <cylinderGeometry args={[0.75, 0.75, width * 0.6, 10]} />
        <primitive object={enclosureMaterial} attach="material" />
      </mesh>

      {/* HV bushings. */}
      {[-1, 0, 1].map((i) => (
        <group key={i} position={[i * (width / 3.4), 0.4 + height * 0.84, depth * 0.15]}>
          <Bushing height={height * 0.5} radius={0.3} />
        </group>
      ))}
    </SelectableAsset>
  );
}

/**
 * Control building: a low block with a parapet, a plant housing set back on the roof, and
 * a door bay. The stepped roofline is what stops it reading as a shipping container.
 */
export function ControlBuilding({ asset }: { asset: Asset }) {
  const { width, depth, height } = asset.footprint;

  return (
    <SelectableAsset asset={asset} ringRadius={30}>
      <Pad width={width + 10} depth={depth + 10} height={0.3} />

      <mesh position={[0, 0.3 + height / 2, 0]}>
        <boxGeometry args={[width, height, depth]} />
        <primitive object={concreteMaterial} attach="material" />
        <Edges threshold={20} color={EDGE_COLOUR} />
      </mesh>

      {/* Parapet. */}
      <mesh position={[0, 0.3 + height + 0.35, 0]}>
        <boxGeometry args={[width + 0.8, 0.7, depth + 0.8]} />
        <primitive object={concreteMaterial} attach="material" />
        <Edges threshold={20} color={EDGE_COLOUR} />
      </mesh>

      {/* Roof plant. */}
      <mesh position={[width * 0.22, 0.3 + height + 1.4, -depth * 0.15]}>
        <boxGeometry args={[width * 0.3, 1.8, depth * 0.4]} />
        <primitive object={enclosureMaterial} attach="material" />
        <Edges threshold={20} color={EDGE_COLOUR} />
      </mesh>

      {/* Entrance canopy. */}
      <mesh position={[-width * 0.28, 0.3 + height * 0.62, depth / 2 + 1.4]}>
        <boxGeometry args={[width * 0.26, 0.24, 2.8]} />
        <primitive object={steelMaterial} attach="material" />
      </mesh>
    </SelectableAsset>
  );
}

/** Inverter station: a skid-mounted cabinet run with a ventilation hood. */
export function InverterStation({ asset }: { asset: Asset }) {
  const { width, depth, height } = asset.footprint;

  return (
    <SelectableAsset asset={asset} ringRadius={14}>
      <Pad width={width + 4} depth={depth + 4} height={0.25} />

      <mesh position={[0, 0.25 + height / 2, 0]}>
        <boxGeometry args={[width, height, depth]} />
        <primitive object={enclosureMaterial} attach="material" />
        <Edges threshold={20} color={EDGE_COLOUR} />
      </mesh>

      {/* Cabinet divisions, so it reads as a run of units rather than one slab. */}
      {[-1, 0, 1].map((i) => (
        <mesh key={i} position={[i * (width / 3.2), 0.25 + height / 2, depth / 2 + 0.04]}>
          <boxGeometry args={[0.1, height * 0.9, 0.08]} />
          <primitive object={steelMaterial} attach="material" />
        </mesh>
      ))}

      {/* Ventilation hood. */}
      <mesh position={[0, 0.25 + height + 0.28, 0]}>
        <boxGeometry args={[width * 0.8, 0.5, depth * 0.7]} />
        <primitive object={steelMaterial} attach="material" />
        <Edges threshold={20} color={EDGE_COLOUR} />
      </mesh>
    </SelectableAsset>
  );
}

/** Battery storage: a row of containerised units with HVAC on the long face. */
export function BatteryStorage({ asset }: { asset: Asset }) {
  const { width, depth, height } = asset.footprint;
  const units = 4;
  const unitWidth = width / units - 1.2;

  return (
    <SelectableAsset asset={asset} ringRadius={30}>
      <Pad width={width + 8} depth={depth + 8} height={0.3} />

      {Array.from({ length: units }, (_, i) => (
        <group key={i} position={[(i - (units - 1) / 2) * (width / units), 0.3, 0]}>
          <mesh position={[0, height / 2, 0]}>
            <boxGeometry args={[unitWidth, height, depth]} />
            <primitive object={enclosureMaterial} attach="material" />
            <Edges threshold={20} color={EDGE_COLOUR} />
          </mesh>
          {/* HVAC units on the south face. */}
          {[-1, 1].map((s) => (
            <mesh key={s} position={[s * unitWidth * 0.25, height * 0.62, depth / 2 + 0.35]}>
              <boxGeometry args={[unitWidth * 0.3, height * 0.4, 0.7]} />
              <primitive object={steelMaterial} attach="material" />
            </mesh>
          ))}
        </group>
      ))}
    </SelectableAsset>
  );
}

/** Vehicle gate: sliding barrier, posts, and a guard cabin. */
export function Gate({ asset }: { asset: Asset }) {
  return (
    <SelectableAsset asset={asset} ringRadius={14}>
      <Pad width={14} depth={20} height={0.25} />

      {[-1, 1].map((s) => (
        <mesh key={s} position={[0, 2, s * 8]}>
          <boxGeometry args={[0.5, 4, 0.5]} />
          <primitive object={steelMaterial} attach="material" />
          <Edges threshold={20} color={EDGE_COLOUR} />
        </mesh>
      ))}

      {/* Barrier. */}
      <mesh position={[0, 2.6, 0]}>
        <boxGeometry args={[0.3, 0.3, 15]} />
        <primitive object={steelMaterial} attach="material" />
      </mesh>

      {/* Guard cabin. */}
      <mesh position={[4.5, 1.6, -6]}>
        <boxGeometry args={[4, 3, 3.4]} />
        <primitive object={concreteMaterial} attach="material" />
        <Edges threshold={20} color={EDGE_COLOUR} />
      </mesh>
    </SelectableAsset>
  );
}
