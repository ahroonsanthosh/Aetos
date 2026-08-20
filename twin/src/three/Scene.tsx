'use client';

/**
 * Scene composition.
 *
 * Everything the environment contains, assembled in one readable place. Each layer is a
 * component that reads what it needs from the store — none of them is passed simulated
 * data, and none of them knows a simulator exists.
 *
 * Lighting is minimal and cool. There is no sun: this is a command visualisation, not a
 * daylight render, and the physical plant is meant to read as dark structure with the
 * system's understanding drawn over it.
 */

import { useEffect, useMemo } from 'react';
import { useThree } from '@react-three/fiber';
import { Fog } from 'three';

import { HANDLED_ASSET_TYPES } from '@/site/assetTypes';
import { palette } from '@/lib/palette';
import { useAetos, useScenePresentation } from '@/state/store';
import { Atmosphere } from './Atmosphere';
import { CameraRig } from './CameraRig';
import { CoverageMap } from './sensors/CoverageMap';
import { GenericAsset } from './site/GenericAsset';
import { LabelDeclutter } from './labels/LabelDeclutter';
import { Labels } from './site/Labels';
import { SensorNodes } from './sensors/SensorNodes';
import { SolarField } from './site/SolarField';
import { Substation } from './site/Substation';
import { Terrain } from './site/Terrain';
import { Tracks } from './tracks/Tracks';
import { ProtectedZones } from './zones/ProtectedZones';
import {
  BatteryStorage,
  ControlBuilding,
  Gate,
  InverterStation,
  Transformer,
} from './site/Structures';

export function Scene() {
  const site = useAetos((s) => s.site);

  /**
   * Structures grouped by type, so the JSX below stays declarative.
   *
   * `generic` is everything outside `HANDLED_ASSET_TYPES` — an OSM import, a traced
   * building, a type someone typed by hand into the editor. It exists so a site built from
   * real data is never missing structures just because this file has not been taught its
   * vocabulary yet.
   */
  const byType = useMemo(() => {
    return {
      substations: site.assets.filter((a) => a.type === 'substation'),
      transformers: site.assets.filter((a) => a.type === 'transformer'),
      buildings: site.assets.filter((a) => a.type === 'control_building'),
      inverters: site.assets.filter((a) => a.type === 'inverter'),
      storage: site.assets.filter((a) => a.type === 'storage'),
      gates: site.assets.filter((a) => a.type === 'gate'),
      generic: site.assets.filter((a) => !HANDLED_ASSET_TYPES.has(a.type)),
    };
  }, [site.assets]);

  return (
    <>
      <SceneFog />
      <CameraRig />

      {/* Cool ambient plus two rim lights. There is no sun — this is a command
          visualisation, not a daylight render — but the intensities are high because the
          post-processing chain, not the renderer, owns the final tone curve here, so
          brightness has to be authored into the lighting rather than dialled in with
          exposure afterwards. */}
      <ambientLight intensity={1.9} color="#54809a" />
      <directionalLight position={[400, 600, 300]} intensity={2.1} color="#9ecfe2" />
      <directionalLight position={[-500, 300, -400]} intensity={1.05} color="#4a90ad" />
      <hemisphereLight args={['#2f6f88', '#060d14', 1.2]} />

      <group>
        <Terrain site={site} />
        <SolarField blocks={site.arrays} />

        {byType.substations.map((asset) => (
          <Substation key={asset.id} asset={asset} />
        ))}
        {byType.transformers.map((asset) => (
          <Transformer key={asset.id} asset={asset} />
        ))}
        {byType.buildings.map((asset) => (
          <ControlBuilding key={asset.id} asset={asset} />
        ))}
        {byType.inverters.map((asset) => (
          <InverterStation key={asset.id} asset={asset} />
        ))}
        {byType.storage.map((asset) => (
          <BatteryStorage key={asset.id} asset={asset} />
        ))}
        {byType.gates.map((asset) => (
          <Gate key={asset.id} asset={asset} />
        ))}
        {byType.generic.map((asset) => (
          <GenericAsset key={asset.id} asset={asset} />
        ))}

        <SensorNodes />
        <CoverageMap />
        <ProtectedZones />
        <Tracks />
        <Labels />

        {/* Last on purpose: R3F runs useFrame callbacks in mount order, so the label
            placement pass reads anchors that everything above has already updated. */}
        <LabelDeclutter />
      </group>

      <Atmosphere />
    </>
  );
}

/**
 * Distance fog, sized to the current site.
 *
 * Set on the scene object directly via `useThree` rather than in `Canvas`'s `onCreated`:
 * that callback fires once, at mount, and a site loaded afterwards would be stuck fogged
 * for whatever the demo site's distances happened to be — visibly wrong for a much larger
 * or smaller import.
 */
function SceneFog() {
  const scene = useThree((s) => s.scene);
  const presentation = useScenePresentation();

  useEffect(() => {
    scene.fog = new Fog(palette.abyss, presentation.fogNear, presentation.fogFar);
    return () => {
      scene.fog = null;
    };
  }, [scene, presentation.fogNear, presentation.fogFar]);

  return null;
}
