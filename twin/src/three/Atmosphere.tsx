'use client';

/**
 * Atmosphere.
 *
 * A very small amount of drifting particulate, and nothing else. Its only job is to give
 * the volume above the site some depth cue so that a track at 40 m and a track at 140 m
 * are distinguishable without reading the label.
 *
 * Deliberately sparse and dim. The brief rules out "restrained atmospheric particles"
 * becoming a snow globe, and particles are the single easiest way to make a serious
 * interface look like a screensaver.
 */

import { useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import { AdditiveBlending, BufferGeometry, Float32BufferAttribute, type Points } from 'three';

import { palette } from '@/lib/palette';

const COUNT = 520;
const SPREAD = 1500;
const CEILING = 320;

export function Atmosphere() {
  const points = useRef<Points>(null);

  const geometry = useMemo(() => {
    const positions = new Float32Array(COUNT * 3);
    for (let i = 0; i < COUNT; i++) {
      positions[i * 3] = (Math.random() - 0.5) * SPREAD * 2;
      // Biased toward the lower airspace, where the tracks actually are.
      positions[i * 3 + 1] = Math.pow(Math.random(), 1.7) * CEILING + 6;
      positions[i * 3 + 2] = (Math.random() - 0.5) * SPREAD * 2;
    }

    const g = new BufferGeometry();
    g.setAttribute('position', new Float32BufferAttribute(positions, 3));
    return g;
  }, []);

  // A slow yaw rather than per-particle motion: one matrix update per frame instead of
  // 520 buffer writes, and at this density the difference is invisible.
  useFrame((_, dt) => {
    if (points.current) points.current.rotation.y += dt * 0.006;
  });

  return (
    <points ref={points} geometry={geometry} frustumCulled={false}>
      <pointsMaterial
        size={1.9}
        sizeAttenuation
        color={palette.holoDim}
        transparent
        opacity={0.28}
        depthWrite={false}
        blending={AdditiveBlending}
      />
    </points>
  );
}
