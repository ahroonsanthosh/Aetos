'use client';

/**
 * Camera.
 *
 * The brief asks for movement that feels "polished and deliberate", which in practice
 * means three things: never cut, never overshoot, and never take control away from the
 * operator for longer than the move itself.
 *
 * So a focus change starts a timed flight that eases the camera into place and then hands
 * control straight back to the orbit controls. Selecting a moving track additionally
 * *follows* it — translating both the camera and its target by the track's per-frame
 * delta, which keeps the operator's chosen viewing angle intact while the subject moves.
 * Snapping the camera to a new angle every frame would be the obvious implementation and
 * would be unusable.
 */

import { useEffect, useRef } from 'react';
import { useFrame, useThree } from '@react-three/fiber';
import { OrbitControls } from '@react-three/drei';
import { Vector3 } from 'three';
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib';

import type { ViewMode } from '@/types';
import { smoothstep } from '@/lib/geometry';
import { interpolatedPosition } from '@/state/buffer';
import { useAetos } from '@/state/store';
import { SITE_EXTENT } from '@/site/demo-site';

/**
 * Distance and pitch the camera settles at for each mode, when nothing is selected.
 *
 * Distances are set from the site's actual extent rather than picked by eye. The site is
 * roughly 940 m by 680 m, and at a 42° vertical field of view the visible height at the
 * target is 2·d·tan(21°) — so an overview that frames the whole site with margin needs
 * about 1,900 m, not the 1,250 m that put the fence line off the edge of the screen.
 */
const MODE_POSE: Record<ViewMode, { distance: number; polar: number }> = {
  overview: { distance: 1900, polar: 0.86 },
  tactical: { distance: 1150, polar: 1.0 },
  threat: { distance: 900, polar: 1.1 },
  // System view pulls back further and looks down, because coverage is read as a map.
  system: { distance: 2300, polar: 0.6 },
};

const FLIGHT_SECONDS = 1.25;

/**
 * What the camera looks at with nothing selected.
 *
 * Not the datum. The left and right panels occupy 260 px and 330 px of a shared frame, so
 * the free area's centre sits slightly left of the viewport's; aiming a little east of the
 * datum puts the site in the middle of the space the operator can actually see, and puts
 * the substation — the asset they will look at most — clear of the context panel.
 */
const SITE_CENTRE = new Vector3(40, 0, 20);

export function CameraRig() {
  const controls = useRef<OrbitControlsImpl>(null);
  const camera = useThree((s) => s.camera);

  const selection = useAetos((s) => s.selection);
  const mode = useAetos((s) => s.mode);
  const site = useAetos((s) => s.site);

  /** Active flight, if any. */
  const flight = useRef<{
    fromTarget: Vector3;
    toTarget: Vector3;
    fromPosition: Vector3;
    toPosition: Vector3;
    elapsed: number;
  } | null>(null);

  /** Last known position of a followed track, for delta-based following. */
  const followed = useRef<Vector3 | null>(null);

  /** Compute a camera position that looks at `target` from the current-ish angle. */
  const poseFor = (target: Vector3, distance: number, polar: number): Vector3 => {
    // Preserve the operator's current azimuth so a focus change never spins the world.
    const current = camera.position.clone().sub(controls.current?.target ?? new Vector3());
    const azimuth = Math.atan2(current.x, current.z) || Math.PI * 0.28;

    return new Vector3(
      target.x + Math.sin(azimuth) * Math.sin(polar) * distance,
      target.y + Math.cos(polar) * distance,
      target.z + Math.cos(azimuth) * Math.sin(polar) * distance,
    );
  };

  const flyTo = (target: Vector3, distance: number, polar: number) => {
    const c = controls.current;
    if (!c) return;
    flight.current = {
      fromTarget: c.target.clone(),
      toTarget: target.clone(),
      fromPosition: camera.position.clone(),
      toPosition: poseFor(target, distance, polar),
      elapsed: 0,
    };
  };

  // --- Focus changes -------------------------------------------------------
  useEffect(() => {
    const pose = MODE_POSE[mode];
    followed.current = null;

    if (!selection) {
      flyTo(SITE_CENTRE, pose.distance, pose.polar);
      return;
    }

    if (selection.kind === 'asset') {
      const asset = site.assets.find((a) => a.id === selection.id);
      if (!asset) return;
      // Frame the asset by its own size, so a transformer and a solar array block are
      // both filled sensibly rather than one of them being a speck.
      const span = Math.max(asset.footprint.width, asset.footprint.depth);
      flyTo(
        new Vector3(asset.position.e, asset.footprint.height * 0.6, -asset.position.n),
        Math.max(span * 3.4, 190),
        1.22,
      );
      return;
    }

    if (selection.kind === 'sensor') {
      const sensor = site.sensors.find((s) => s.id === selection.id);
      if (!sensor) return;
      flyTo(
        new Vector3(sensor.position.e, sensor.mastHeight * 0.7, -sensor.position.n),
        170,
        1.2,
      );
      return;
    }

    if (selection.kind === 'track') {
      const p = interpolatedPosition(selection.id);
      if (!p) return;
      const track = new Vector3(p.e, p.u, -p.n);
      followed.current = track.clone();

      // Frame the track *and* the site, not the track alone. Selecting a track is a
      // question about a relationship — this object, against that asset — and a camera
      // that flies to the object and fills the screen with it has answered a different,
      // less useful question. So the look-at point is pulled back toward the site and the
      // distance opens up with how far out the track is.
      const target = track.clone().lerp(SITE_CENTRE, 0.38);
      const spread = track.distanceTo(SITE_CENTRE);
      flyTo(target, Math.max(spread * 1.15, 700), 1.02);
    }
    // `site` is static; re-running on mode or selection is the whole intent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection?.kind, selection?.id, mode]);

  // --- Per-frame -----------------------------------------------------------
  useFrame((_, dt) => {
    const c = controls.current;
    if (!c) return;

    const active = flight.current;
    if (active) {
      active.elapsed += dt;
      const raw = Math.min(active.elapsed / FLIGHT_SECONDS, 1);
      // Smoothstep rather than linear: the ease-out at the end is what makes an
      // automated camera move read as deliberate rather than mechanical.
      const t = smoothstep(0, 1, raw);

      c.target.lerpVectors(active.fromTarget, active.toTarget, t);
      camera.position.lerpVectors(active.fromPosition, active.toPosition, t);

      if (raw >= 1) flight.current = null;
      c.update();
      return;
    }

    // Follow a selected track by translation only, so the operator keeps their angle.
    if (followed.current && selection?.kind === 'track') {
      const p = interpolatedPosition(selection.id);
      if (p) {
        const next = new Vector3(p.e, p.u, -p.n);
        const delta = next.clone().sub(followed.current);
        c.target.add(delta);
        camera.position.add(delta);
        followed.current = next;
        c.update();
      }
    }
  });

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enableDamping
      dampingFactor={0.075}
      rotateSpeed={0.55}
      panSpeed={0.8}
      zoomSpeed={0.85}
      minDistance={40}
      maxDistance={SITE_EXTENT * 6}
      // Stop just short of horizontal: letting the camera go under the ground plane is
      // the fastest way to make a 3D interface feel unfinished.
      maxPolarAngle={Math.PI * 0.487}
      target={[0, 40, 0]}
    />
  );
}
