'use client';

/**
 * Selection behaviour for a physical asset.
 *
 * Wraps a structure's geometry, owns its hit testing, and draws the ground ring that says
 * "this is a thing the system knows about". Keeping this in one component means every
 * asset on the site responds to the operator identically, and adding a new structure type
 * does not mean re-implementing hover, click and criticality treatment.
 */

import { useMemo, useRef, useState, type ReactNode } from 'react';
import { useFrame, type ThreeEvent } from '@react-three/fiber';
import type { Group, Mesh } from 'three';
import { AdditiveBlending, DoubleSide } from 'three';

import type { Asset } from '@/types';
import { palette, threatColour } from '@/lib/palette';
import { damp } from '@/lib/geometry';
import { useAetos } from '@/state/store';
import { toWorld, yawFor } from '../frame';

interface Props {
  asset: Asset;
  children: ReactNode;
  /** Radius of the ground ring. Defaults to the asset footprint. */
  ringRadius?: number;
}

export function SelectableAsset({ asset, children, ringRadius }: Props) {
  const group = useRef<Group>(null);
  const ring = useRef<Mesh>(null);
  const [hovered, setHovered] = useState(false);

  const select = useAetos((s) => s.select);
  const selected = useAetos((s) => s.selection?.kind === 'asset' && s.selection.id === asset.id);
  const mode = useAetos((s) => s.mode);
  const liveThreats = useAetos((s) => s.snapshot?.assets[asset.id]?.activeThreats.length ?? 0);
  const threatLevel = useAetos((s) => {
    const ids = s.snapshot?.assets[asset.id]?.activeThreats ?? [];
    if (ids.length === 0) return null;
    const tracks = s.snapshot?.tracks.filter((t) => ids.includes(t.trackId)) ?? [];
    if (tracks.some((t) => t.threat.level === 'critical')) return 'critical' as const;
    if (tracks.some((t) => t.threat.level === 'high')) return 'high' as const;
    return 'medium' as const;
  });

  const radius = ringRadius ?? Math.max(asset.footprint.width, asset.footprint.depth) * 0.62 + 6;

  const colour = threatLevel
    ? threatColour[threatLevel]
    : selected
      ? palette.select
      : asset.criticality >= 8
        ? palette.holo
        : palette.holoDim;

  /**
   * Criticality earns visual weight even at rest. A 10/10 substation is faintly ringed
   * before anything happens; a 4/10 array block is not.
   */
  const restOpacity = useMemo(() => {
    if (asset.criticality >= 9) return 0.3;
    if (asset.criticality >= 7) return 0.2;
    if (asset.criticality >= 5) return 0.1;
    return 0.05;
  }, [asset.criticality]);

  useFrame((_, dt) => {
    if (!ring.current) return;

    // Non-critical plant recedes in threat mode so the tactical picture is legible.
    const suppressed = mode === 'threat' && asset.criticality < 7 && !selected;

    const target = suppressed
      ? 0.02
      : selected
        ? 0.85
        : hovered
          ? 0.6
          : threatLevel
            ? 0.55 + (liveThreats > 1 ? 0.2 : 0)
            : restOpacity;

    const material = ring.current.material as { opacity: number };
    material.opacity = damp(material.opacity, target, 0.0015, dt);

    // A threatened asset's ring breathes. Motion, not just colour, carries the signal.
    const pulse = threatLevel ? 1 + Math.sin(performance.now() * 0.004) * 0.035 : 1;
    ring.current.scale.setScalar(pulse);
  });

  const onClick = (event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    select({ kind: 'asset', id: asset.id });
  };

  return (
    <group
      ref={group}
      position={toWorld(asset.position)}
      rotation={[0, yawFor(asset.heading), 0]}
      onClick={onClick}
      onPointerOver={(e) => {
        e.stopPropagation();
        setHovered(true);
        document.body.style.cursor = 'pointer';
      }}
      onPointerOut={() => {
        setHovered(false);
        document.body.style.cursor = 'default';
      }}
    >
      {children}

      {/* Ground ring. Drawn just above the terrain to avoid z-fighting with it. */}
      <mesh ref={ring} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.12, 0]}>
        <ringGeometry args={[radius, radius + 1.6, 72]} />
        <meshBasicMaterial
          color={colour}
          transparent
          opacity={restOpacity}
          blending={AdditiveBlending}
          depthWrite={false}
          side={DoubleSide}
        />
      </mesh>
    </group>
  );
}
