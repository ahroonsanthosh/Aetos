'use client';

/**
 * Protected zones as volumes.
 *
 * The brief is explicit that these must not be flat circles, and the reason is
 * operational rather than aesthetic: the thing being defended against flies. A zone that
 * stops at the ground cannot express "it is about to cross the boundary at 60 m", which is
 * the only version of that sentence worth showing.
 *
 * Emphasis is computed by the engine, not here. This component's whole job is to turn one
 * number into a visual state, smoothly, so that a zone becoming relevant reads as the
 * environment responding rather than a layer being switched on.
 */

import { forwardRef, useImperativeHandle, useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import { Line } from '@react-three/drei';
import { Color, type ShaderMaterial } from 'three';

import type { Zone } from '@/types';
import {
  palette,
  zoneActiveColour,
  zoneBaseOpacity,
  zoneMaxOpacity,
  zoneRestColour,
} from '@/lib/palette';
import { damp, smoothstep } from '@/lib/geometry';
import { useAetos, useLiveZones } from '@/state/store';
import { polygonWalls } from '../lib/extrude';
import { createVolumeMaterial } from '../materials/volume';

/**
 * Owns one zone's shader material so `useFrame` can drive its uniforms directly. R3F
 * handles the attach lifecycle; the ref hands the material back to the animating parent.
 */
const ZoneMaterial = forwardRef<
  ShaderMaterial,
  { colour: string; height: number; opacity: number }
>(function ZoneMaterial({ colour, height, opacity }, ref) {
  const material = useMemo(
    () => createVolumeMaterial({ color: colour, opacity, height, rim: 1 }),
    [colour, opacity, height],
  );

  useImperativeHandle(ref, () => material, [material]);

  return <primitive object={material} attach="material" />;
});

export function ProtectedZones() {
  const zones = useLiveZones();
  const visible = useAetos((s) => s.layers.zones);

  if (!visible) return null;

  return (
    <group>
      {zones.map((zone) => (
        <ProtectedZone key={zone.id} zone={zone} />
      ))}
    </group>
  );
}

function ProtectedZone({ zone }: { zone: Zone }) {
  const materialRef = useRef<ShaderMaterial>(null);
  const smoothed = useRef(0);

  const select = useAetos((s) => s.select);
  const selectedAssetId = useAetos((s) =>
    s.selection?.kind === 'asset' ? s.selection.id : null,
  );

  const height = zone.ceiling - zone.floor;
  const geometry = useMemo(() => polygonWalls(zone.polygon, height), [zone.polygon, height]);

  const baseOpacity = zoneBaseOpacity[zone.zoneClass];
  const maxOpacity = zoneMaxOpacity[zone.zoneClass];
  const restColour = zoneRestColour[zone.zoneClass];

  // Endpoints of the colour ramp, built once. `uColor` is interpolated between them each
  // frame, so a zone warms up as something is predicted into it and cools back down after.
  const ramp = useMemo(
    () => ({
      rest: new Color(restColour),
      active: new Color(zoneActiveColour[zone.zoneClass]),
    }),
    [restColour, zone.zoneClass],
  );

  const floorOutline = useMemo(() => {
    const points = zone.polygon.map(
      (p) => [p.e, zone.floor + 0.3, -p.n] as [number, number, number],
    );
    const first = points[0];
    if (first) points.push(first);
    return points;
  }, [zone.polygon, zone.floor]);

  /**
   * Selecting the asset a zone guards brings that zone forward. The relationship between
   * an asset and the volume protecting it should be visible, not inferred from a panel.
   */
  const selectedForAsset = selectedAssetId !== null && zone.protectsAssetId === selectedAssetId;

  useFrame((_, dt) => {
    const material = materialRef.current;
    if (!material) return;

    const target = Math.max(zone.emphasis, selectedForAsset ? 0.55 : 0);
    // Rises quickly, falls slowly. A zone should assert itself the moment something is
    // predicted into it, and settle back without flickering as the prediction wobbles.
    const rate = target > smoothed.current ? 0.0006 : 0.06;
    smoothed.current = damp(smoothed.current, target, rate, dt);

    const e = smoothed.current;
    material.uniforms.uOpacity!.value = baseOpacity + e * (maxOpacity - baseOpacity);
    (material.uniforms.uColor!.value as Color).lerpColors(
      ramp.rest,
      ramp.active,
      smoothstep(0.12, 0.65, e),
    );
    material.uniforms.uSweep!.value = e > 0.25 ? Math.min((e - 0.25) / 0.4, 1) : 0;
    material.uniforms.uTime!.value += dt * (1 + e * 2.5);
  });

  return (
    <group position={[0, zone.floor, 0]}>
      <mesh
        geometry={geometry}
        onClick={(event) => {
          // A zone is a way of reaching the asset it protects, never a selection itself.
          if (!zone.protectsAssetId) return;
          event.stopPropagation();
          select({ kind: 'asset', id: zone.protectsAssetId });
        }}
      >
        <ZoneMaterial ref={materialRef} colour={restColour} height={height} opacity={baseOpacity} />
      </mesh>

      <Line
        points={floorOutline}
        color={zone.zoneClass === 'critical' ? restColour : palette.holoDim}
        lineWidth={zone.zoneClass === 'critical' ? 1.3 : 0.9}
        transparent
        opacity={zone.zoneClass === 'critical' ? 0.5 : 0.28}
      />
    </group>
  );
}
