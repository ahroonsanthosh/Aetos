'use client';

/**
 * The canvas and its post-processing.
 *
 * Bloom is the only effect doing real work, and it is tuned to catch emissive markers and
 * volume rims while leaving the dark structure alone — that threshold is what separates a
 * holographic look from a blurry one. Vignette is very light. There is no chromatic
 * aberration, no film grain, no scanline overlay: all three are the standard shortcuts to
 * "sci-fi" and all three make text harder to read, which disqualifies them from a command
 * interface.
 */

import { Suspense } from 'react';
import { Canvas } from '@react-three/fiber';
import { AdaptiveDpr, Preload } from '@react-three/drei';
import { Bloom, EffectComposer, Vignette } from '@react-three/postprocessing';
import { BlendFunction, KernelSize } from 'postprocessing';
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
        // ACES keeps bright emissive markers from clipping to white under bloom, but its
        // toe crushes shadows — and this whole scene lives in the range the toe eats. The
        // exposure compensates: without it the site renders as near-black on black and the
        // physical plant simply is not there.
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

      <EffectComposer enableNormalPass={false} multisampling={4}>
        <Bloom
          // High threshold: only the deliberately emissive things glow. Drop this and the
          // whole site starts to shimmer, which reads as cheap immediately.
          luminanceThreshold={0.62}
          luminanceSmoothing={0.28}
          intensity={0.95}
          kernelSize={KernelSize.LARGE}
          mipmapBlur
        />
        <Vignette offset={0.32} darkness={0.42} blendFunction={BlendFunction.NORMAL} />
      </EffectComposer>

      {/* Drops resolution rather than frame rate when the GPU is struggling. A command
          display that stutters is worse than one that is slightly soft. */}
      <AdaptiveDpr pixelated={false} />
    </Canvas>
  );
}
