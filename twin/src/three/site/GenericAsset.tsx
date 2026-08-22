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
 * plain box, the same as the demo site's own hand-authored assets. Either shape then gets
 * `BuildingDetail` layered on top, at no cost to how the asset was authored — see that
 * file for why.
 */

import { useMemo } from 'react';
import { Edges } from '@react-three/drei';
import type { BufferGeometry } from 'three';

import type { Asset, Ground } from '@/types';
import { EDGE_COLOUR, concreteMaterial } from '../materials/structure';
import { extrudedPolygon } from '../lib/extrude';
import { BuildingDetail } from './buildingDetail';
import { Pad } from './parts';
import { SelectableAsset } from './SelectableAsset';

export function GenericAsset({ asset }: { asset: Asset }) {
  const { width, depth, height } = asset.footprint;

  const polygon = asset.footprintPolygon;
  const hasPolygon = !!polygon && polygon.length >= 3;

  const polygonGeometry = useMemo<BufferGeometry | null>(() => {
    if (!hasPolygon) return null;
    return extrudedPolygon(polygon!, height);
  }, [hasPolygon, polygon, height]);

  // The box variant sits on a raised pad; the polygon variant does not (see below). Detail
  // is generated relative to "the building's own base", so it is wrapped at whichever
  // height that base actually sits at, rather than baking the pad into every measurement.
  const outline: Ground[] = hasPolygon
    ? polygon!
    : [
        { e: -width / 2, n: -depth / 2 },
        { e: width / 2, n: -depth / 2 },
        { e: width / 2, n: depth / 2 },
        { e: -width / 2, n: depth / 2 },
      ];
  const baseY = hasPolygon ? 0 : 0.25;

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
      <group position={[0, baseY, 0]}>
        <BuildingDetail outline={outline} height={height} seed={asset.id} />
      </group>
    </SelectableAsset>
  );
}
