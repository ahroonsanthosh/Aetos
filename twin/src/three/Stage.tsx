'use client';

/**
 * The canvas.
 *
 * No post-processing. Bloom and vignette made markers glow and edges darken, which reads as a
 * stock "sci-fi" look and softens text and edges; markers now carry their meaning in colour
 * and shape alone.
 */

import { Suspense } from 'react';
import { Canvas } from '@react-three/fiber';
import { AdaptiveDpr, Preload } from '@react-three/drei';
import { ACESFilmicToneMapping } from 'three';

import { useAetos } from '@/state/store';
import { Scene } from './Scene';

export function Stage() {
  const select = useAetos((s) => s.select);

  return (
    <Canvas
      dpr={[1, 1.75]}
      gl={{
        antialias: true,
        powerPreference: 'high-performance',
        // ACES's toe crushes shadows, and this whole scene lives in the range the toe eats.
        // The exposure compensates: without it the site renders as near-black on black.
        toneMapping: ACESFilmicToneMapping,
        toneMappingExposure: 1.55,
      }}
      camera={{ fov: 42, near: 1, far: 12000, position: [640, 470, 820] }}
      onPointerMissed={() => select(null)}
      onCreated={({ scene }) => {
        scene.background = null;
      }}
    >
      <Suspense fallback={null}>
        <Scene />
        <Preload all />
      </Suspense>


      {/* Drops resolution rather than frame rate when the GPU is struggling. A command
          display that stutters is worse than one that is slightly soft. */}
      <AdaptiveDpr pixelated={false} />
    </Canvas>
  );
}
