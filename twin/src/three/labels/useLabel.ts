'use client';

/**
 * React binding for the label registry.
 *
 * The handle is stable for the life of the component and is released on unmount, so a track
 * that drops out of the picture stops competing for screen space in the same frame it stops
 * being drawn.
 */

import { useEffect, useMemo } from 'react';

import { acquireLabel, type LabelHandle } from './registry';

export function useLabel(priority: number): LabelHandle {
  const handle = useMemo(() => acquireLabel(priority), []);

  // Priority changes as things change — a track escalates, an asset comes under threat, the
  // operator selects something. Kept in an effect so the registry is never written during
  // render.
  useEffect(() => {
    handle.setPriority(priority);
  }, [handle, priority]);

  // Attach and release as a pair, so a Strict Mode remount re-enters the pass.
  useEffect(() => {
    handle.attach();
    return () => handle.release();
  }, [handle]);

  return handle;
}
