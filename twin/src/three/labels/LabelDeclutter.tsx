'use client';

/**
 * Runs the label placement pass, once per frame, for the whole scene.
 *
 * Mounted last in `Scene` on purpose. R3F runs `useFrame` callbacks in subscription order,
 * which follows mount order, so placing this component after everything that moves a label
 * anchor means it reads positions that are already current for this frame.
 *
 * It deliberately does not use a `useFrame` render priority to force that ordering: any
 * non-zero priority hands the render loop over to the caller, and this component has no
 * business taking responsibility for rendering the scene.
 */

import { useEffect } from 'react';
import { useFrame, useThree } from '@react-three/fiber';

import { declutter } from './registry';

export function LabelDeclutter() {
  const camera = useThree((s) => s.camera);
  const size = useThree((s) => s.size);

  // A resize changes every projected position at once; clearing the cached box widths on
  // the next pass keeps the collision maths honest about the new viewport.
  useEffect(() => {
    declutter(camera, size.width, size.height);
  }, [camera, size.width, size.height]);

  useFrame(() => {
    declutter(camera, size.width, size.height);
  });

  return null;
}

/**
 * Hook form of `acquireLabel`, handling the React lifecycle.
 *
 * Returns a stable handle for the life of the component. The caller mutates `handle.world`
 * wherever it already tracks position — per-frame for a moving track, once for a fixed
 * asset — and attaches `handle.setElement` as a ref callback on the element it wants
 * placed.
 */
export { useLabel } from './useLabel';
