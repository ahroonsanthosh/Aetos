/**
 * Deriving how the scene presents itself from the site's actual size.
 *
 * Every distance in `CameraRig.tsx`, `Terrain.tsx` and `Stage.tsx` used to be a number
 * tuned by eye against one 940×680 m demo facility. That was fine while there was only one
 * site; it breaks the moment a real one is loaded, because a 200 m compound and a 4 km
 * refinery are not the same problem scaled — an overview distance and a grid spacing tuned
 * for one puts the other's fence line off the edge of the screen or buries it under a grid
 * too fine to read.
 *
 * This file is the fix: one function computes a site's extent, everything else — camera
 * poses, fog distance, grid spacing, coverage sampling resolution — is derived from that
 * extent by the same ratios the demo site was hand-tuned to, so loading a different site
 * changes the numbers without anyone retuning them by hand.
 */

import type { Site, ViewMode } from '@/types';
import { horizontalDistance } from '@/lib/geometry';

/**
 * Half-extent of the *physical* site, metres — the radius of the smallest circle centred on
 * the datum containing the boundary and every asset.
 *
 * Deliberately excludes sensor coverage. This is the number the camera frames against, and
 * a site's radars routinely reach several times further than its fence: a 500 m compound
 * with 1.5 km radars would otherwise be framed as a 1.5 km site, leaving the actual
 * facility a smudge in the middle of an empty screen. Coverage still has to fit inside the
 * ground plane and the camera's zoom limit — that is `computeSceneExtent` below.
 *
 * Takes only the fields it reads, not a full `Site` — the zone generator calls this before
 * a draft has sensors, an id, or a name.
 */
export function computeSiteExtent(site: Pick<Site, 'boundary' | 'assets'>): number {
  let extent = 60; // floor, so a nearly empty site still renders sensible defaults.

  for (const p of site.boundary) {
    extent = Math.max(extent, Math.hypot(p.e, p.n));
  }
  for (const asset of site.assets) {
    const span = Math.max(asset.footprint.width, asset.footprint.depth) / 2;
    extent = Math.max(extent, horizontalDistance(asset.position, { e: 0, n: 0, u: 0 }) + span);
  }

  return extent;
}

/**
 * Half-extent of everything that gets *drawn*, including sensor coverage envelopes.
 *
 * The ground plane, the fog and the camera's maximum zoom-out are sized from this, so that
 * turning on the sensor coverage layer never reveals a dome hanging over the void past the
 * edge of the world.
 */
export function computeSceneExtent(site: Pick<Site, 'boundary' | 'assets' | 'sensors'>): number {
  let extent = computeSiteExtent(site);

  for (const sensor of site.sensors) {
    extent = Math.max(
      extent,
      horizontalDistance(sensor.position, { e: 0, n: 0, u: 0 }) + sensor.coverage.maxRange,
    );
  }

  return extent;
}

export interface ScenePresentation {
  /** Physical site radius — what the camera frames against. */
  siteExtent: number;
  /** Physical site plus sensor coverage — what has to fit inside the drawn world. */
  sceneExtent: number;
  groundExtent: number;
  fogNear: number;
  fogFar: number;
  gridMinor: number;
  gridMajor: number;
  gridFadeStart: number;
  gridFadeEnd: number;
  coverageExtent: number;
  coverageResolution: number;
  cameraMaxDistance: number;
  cameraPoses: Record<ViewMode, { distance: number; polar: number }>;
}

/**
 * Snap a spacing to a round number from a fixed ladder, rather than an arbitrary fraction of
 * the site's size. A grid at 37 m intervals reads as noise; one at 25, 50 or 100 m reads as
 * a scale the eye can use.
 */
const SPACING_LADDER = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000];

function snapSpacing(target: number): number {
  let best = SPACING_LADDER[0]!;
  let bestError = Infinity;
  for (const candidate of SPACING_LADDER) {
    const error = Math.abs(Math.log(candidate / target));
    if (error < bestError) {
      bestError = error;
      best = candidate;
    }
  }
  return best;
}

/**
 * The demo site's extent (~660 m, boundary furthest point) is the baseline every ratio
 * below was tuned against — `docs/` calls these numbers out explicitly rather than leaving
 * a future reader to reverse-engineer 1900/520 from a diff.
 */
const BASELINE_EXTENT = 660;

export function deriveScenePresentation(site: Site): ScenePresentation {
  const siteExtent = computeSiteExtent(site);
  const sceneExtent = computeSceneExtent(site);
  const scale = siteExtent / BASELINE_EXTENT;

  const cameraPoses: Record<ViewMode, { distance: number; polar: number }> = {
    overview: { distance: 1900 * scale, polar: 0.86 },
    tactical: { distance: 1150 * scale, polar: 1.0 },
    threat: { distance: 900 * scale, polar: 1.1 },
    system: { distance: 2300 * scale, polar: 0.6 },
  };

  return {
    siteExtent,
    sceneExtent,
    // Sized from the scene extent, not the site's: the ground has to reach past the
    // furthest thing drawn on it, which is usually a sensor's coverage envelope.
    groundExtent: Math.max(2600 * scale, sceneExtent * 2.2),
    fogNear: Math.max(1400 * scale, sceneExtent * 1.4),
    fogFar: Math.max(5200 * scale, sceneExtent * 4),
    gridMinor: snapSpacing(siteExtent / 26),
    gridMajor: snapSpacing(siteExtent / 2.6),
    gridFadeStart: Math.max(800 * scale, sceneExtent),
    gridFadeEnd: Math.max(2400 * scale, sceneExtent * 2.4),
    coverageExtent: Math.max(700 * scale, siteExtent * 1.1),
    // Resolution scales more slowly than extent: a much bigger site should sample a coarser
    // grid rather than multiply the cell count by the square of the scale factor.
    coverageResolution: Math.max(20, snapSpacing(50 * Math.sqrt(scale))),
    cameraMaxDistance: Math.max(sceneExtent * 4, siteExtent * 6, 3200),
    cameraPoses,
  };
}
