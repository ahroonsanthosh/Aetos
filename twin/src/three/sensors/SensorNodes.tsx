'use client';

/**
 * Sensors as physical nodes plus the volume each one can actually see.
 *
 * The coverage shell is the outer envelope of the sensor's declared geometry — sector,
 * elevation band and maximum range — drawn with the same fresnel volume material as the
 * protected zones, because they are the same kind of object: a region of space with a
 * state. Rendering coverage as a dome the operator can fly around is the difference
 * between "radar 02 is offline" and seeing the hole that leaves.
 */

import { useMemo, useRef } from 'react';
import { useFrame, type ThreeEvent } from '@react-three/fiber';
import { Edges } from '@react-three/drei';
import type { Group, Mesh, ShaderMaterial } from 'three';

import type { Sensor } from '@/types';
import { palette, sensorColour } from '@/lib/palette';
import { useAetos, useLiveSensors } from '@/state/store';
import { toWorld } from '../frame';
import { createVolumeMaterial } from '../materials/volume';
import { enclosureMaterial, steelMaterial } from '../materials/structure';
import { Lattice, Pad } from '../site/parts';

export function SensorNodes() {
  const sensors = useLiveSensors();
  const showCoverage = useAetos((s) => s.layers.sensorCoverage);

  return (
    <group>
      {sensors.map((sensor) => (
        <SensorNode key={sensor.id} sensor={sensor} showCoverage={showCoverage} />
      ))}
    </group>
  );
}

function SensorNode({ sensor, showCoverage }: { sensor: Sensor; showCoverage: boolean }) {
  const select = useAetos((s) => s.select);
  const selected = useAetos(
    (s) => s.selection?.kind === 'sensor' && s.selection.id === sensor.id,
  );

  const colour = sensorColour[sensor.status];

  const onClick = (event: ThreeEvent<MouseEvent>) => {
    event.stopPropagation();
    select({ kind: 'sensor', id: sensor.id });
  };

  return (
    <group position={toWorld(sensor.position)}>
      <group
        onClick={onClick}
        onPointerOver={(e) => {
          e.stopPropagation();
          document.body.style.cursor = 'pointer';
        }}
        onPointerOut={() => (document.body.style.cursor = 'default')}
      >
        <Pad width={5} depth={5} height={0.3} />
        <Lattice height={sensor.mastHeight} base={1.9} top={1.1} bay={2.6} legRadius={0.11} />
        <SensorHead sensor={sensor} colour={colour} />
        <StatusBeacon sensor={sensor} colour={colour} selected={selected} />
      </group>

      {showCoverage && sensor.status !== 'offline' && (
        <CoverageShell sensor={sensor} colour={colour} />
      )}
    </group>
  );
}

/** Sensor-specific head geometry. A radar and a camera must not look the same. */
function SensorHead({ sensor, colour }: { sensor: Sensor; colour: string }) {
  const spinner = useRef<Group>(null);

  // A rotating radar array is the clearest possible "this sensor is alive" signal, and it
  // stops when the sensor does.
  useFrame((_, dt) => {
    if (!spinner.current) return;
    const rate = sensor.status === 'offline' ? 0 : sensor.status === 'degraded' ? 0.35 : 1.1;
    spinner.current.rotation.y += rate * dt;
  });

  const y = sensor.mastHeight;

  switch (sensor.type) {
    case 'radar':
      return (
        <group position={[0, y, 0]}>
          <mesh position={[0, 0.35, 0]}>
            <cylinderGeometry args={[0.9, 0.9, 0.7, 12]} />
            <primitive object={enclosureMaterial} attach="material" />
          </mesh>
          <group ref={spinner} position={[0, 1.1, 0]}>
            <mesh>
              <boxGeometry args={[4.2, 0.9, 0.28]} />
              <primitive object={steelMaterial} attach="material" />
              <Edges threshold={20} color={colour} />
            </mesh>
          </group>
        </group>
      );

    case 'camera':
      return (
        <group position={[0, y, 0]}>
          <mesh position={[0, 0.3, 0]}>
            <boxGeometry args={[1.1, 0.6, 1.1]} />
            <primitive object={enclosureMaterial} attach="material" />
          </mesh>
          {/* Dome — the universal PTZ camera cue. */}
          <mesh position={[0, 0.05, 0]}>
            <sphereGeometry args={[0.62, 16, 10, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2]} />
            <meshStandardMaterial color="#16222e" roughness={0.3} metalness={0.25} />
          </mesh>
        </group>
      );

    case 'thermal':
      return (
        <group position={[0, y, 0]}>
          <mesh position={[0, 0.3, 0]}>
            <boxGeometry args={[1.3, 0.75, 0.8]} />
            <primitive object={enclosureMaterial} attach="material" />
            <Edges threshold={20} color={colour} />
          </mesh>
          {/* Germanium lens barrel. */}
          <mesh position={[0, 0.3, 0.6]} rotation={[Math.PI / 2, 0, 0]}>
            <cylinderGeometry args={[0.26, 0.26, 0.5, 12]} />
            <meshStandardMaterial color="#1e2833" roughness={0.28} metalness={0.3} />
          </mesh>
        </group>
      );

    case 'rf':
      return (
        <group position={[0, y, 0]}>
          <mesh position={[0, 0.25, 0]}>
            <cylinderGeometry args={[0.5, 0.5, 0.5, 10]} />
            <primitive object={enclosureMaterial} attach="material" />
          </mesh>
          {/* Crossed dipole array. */}
          {[0, 1, 2, 3].map((i) => {
            const a = (i / 4) * Math.PI * 2;
            return (
              <mesh
                key={i}
                position={[Math.sin(a) * 0.42, 1.3, Math.cos(a) * 0.42]}
              >
                <cylinderGeometry args={[0.045, 0.045, 2, 5]} />
                <primitive object={steelMaterial} attach="material" />
              </mesh>
            );
          })}
        </group>
      );

    default:
      return (
        <mesh position={[0, y + 0.3, 0]}>
          <boxGeometry args={[0.9, 0.6, 0.9]} />
          <primitive object={enclosureMaterial} attach="material" />
        </mesh>
      );
  }
}

/**
 * Status beacon. Offline sensors do not simply go dark — they blink slowly in a dead
 * colour, because a display where "broken" and "not drawn" look alike is worse than none.
 */
function StatusBeacon({
  sensor,
  colour,
  selected,
}: {
  sensor: Sensor;
  colour: string;
  selected: boolean;
}) {
  const mesh = useRef<Mesh>(null);

  useFrame(() => {
    if (!mesh.current) return;
    const t = performance.now() * 0.001;
    const pulse =
      sensor.status === 'offline'
        ? 0.25 + Math.sin(t * 2.2) * 0.22
        : sensor.status === 'degraded'
          ? 0.75 + Math.sin(t * 3.4) * 0.25
          : 0.9 + Math.sin(t * 1.1) * 0.1;

    const material = mesh.current.material as { opacity: number };
    material.opacity = selected ? 1 : pulse;
    mesh.current.scale.setScalar(selected ? 1.5 : 1);
  });

  return (
    <mesh ref={mesh} position={[0, sensor.mastHeight + 2.4, 0]}>
      <sphereGeometry args={[0.32, 10, 8]} />
      <meshBasicMaterial color={selected ? palette.select : colour} transparent opacity={0.9} />
    </mesh>
  );
}

/**
 * The coverage envelope.
 *
 * A partial sphere: `phi` gives the azimuth sector, `theta` the elevation band. three's
 * sphere places phi = 0 along -X, so the mesh is yawed to put the sector centreline on the
 * sensor's declared bearing.
 */
function CoverageShell({ sensor, colour }: { sensor: Sensor; colour: string }) {
  const material = useMemo(() => {
    const c = sensor.coverage;
    return createVolumeMaterial({
      color: colour,
      // Scaled by effectiveness: a degraded sensor's envelope is visibly fainter, so the
      // operator reads capability rather than nameplate.
      opacity: 0.085 * (0.35 + sensor.effectiveness * 0.65),
      height: Math.max(c.maxRange * Math.sin(c.maxElevation), 1),
      rim: 0.35,
      sweep: 0,
    });
  }, [colour, sensor.coverage, sensor.effectiveness]);

  const args = useMemo(() => {
    const c = sensor.coverage;
    const phiLength = c.azimuthWidth;
    const thetaStart = Math.PI / 2 - c.maxElevation;
    const thetaLength = Math.max(c.maxElevation - c.minElevation, 0.02);
    return [
      c.maxRange,
      Math.max(24, Math.round((phiLength / (Math.PI * 2)) * 96)),
      12,
      -phiLength / 2,
      phiLength,
      thetaStart,
      thetaLength,
    ] as const;
  }, [sensor.coverage]);

  // Maps the sector centreline (which starts along -X) onto the declared bearing.
  const yaw = useMemo(() => {
    const b = sensor.coverage.bearing;
    return Math.atan2(-Math.cos(b), -Math.sin(b));
  }, [sensor.coverage.bearing]);

  useFrame((_, dt) => {
    const uniforms = (material as ShaderMaterial).uniforms;
    if (uniforms.uTime) uniforms.uTime.value += dt;
  });

  return (
    <mesh position={[0, sensor.mastHeight, 0]} rotation={[0, yaw, 0]}>
      <sphereGeometry args={args as unknown as [number, number, number, number, number, number, number]} />
      <primitive object={material} attach="material" />
    </mesh>
  );
}
