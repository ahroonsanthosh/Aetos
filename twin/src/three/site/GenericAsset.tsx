'use client';

/**
 * The fallback for any asset type without a bespoke model.
 *
 * `Substation.tsx` and `Structures.tsx` cover the seven types Aetos knows how to draw in
 * detail. Everything else — a building imported from OSM, a shape traced from a site plan,
 * a type someone typed into the editor by hand — comes through here instead. The brief for
 * the demo site was "a substation must look like a substation"; the corollary for real data
 * is that an asset must never look like nothing. A massing box with an edge highlight is a
 * legible, honest representation of "the system knows this building exists, at this
 * location, this large" without pretending to model detail it was never given.
 *
 * Two shapes, depending on what the asset carries: a real traced or imported outline
 * (`footprintPolygon`) is extruded solid; anything with only a bounding `footprint` gets a
 * plain box, the same as the demo site's own hand-authored assets.
 */

import { useMemo } from 'react';
import { Edges } from '@react-three/drei';
import type { BufferGeometry } from 'three';

import type { Asset } from '@/types';
import { EDGE_COLOUR, concreteMaterial } from '../materials/structure';
import { extrudedPolygon } from '../lib/extrude';
import { Pad } from './parts';
import { SelectableAsset } from './SelectableAsset';

export function GenericAsset({ asset }: { asset: Asset }) {
  const { width, depth, height } = asset.footprint;

  const polygonGeometry = useMemo<BufferGeometry | null>(() => {
    if (!asset.footprintPolygon || asset.footprintPolygon.length < 3) return null;
    return extrudedPolygon(asset.footprintPolygon, height);
  }, [asset.footprintPolygon, height]);

  return (
    <SelectableAsset asset={asset} ringRadius={Math.max(width, depth) * 0.62 + 6}>
      {polygonGeometry ? (
        <mesh geometry={polygonGeometry}>
          <primitive object={concreteMaterial} attach="material" />
          <Edges threshold={20} color={EDGE_COLOUR} />
        </mesh>
      ) : (
        <>
          <Pad width={width + 2} depth={depth + 2} height={0.25} />
          <mesh position={[0, 0.25 + height / 2, 0]}>
            <boxGeometry args={[width, height, depth]} />
            <primitive object={concreteMaterial} attach="material" />
            <Edges threshold={20} color={EDGE_COLOUR} />
          </mesh>
        </>
      )}
    </SelectableAsset>
  );
}
