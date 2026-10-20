'use client';

/**
 * Tracks: markers, trails and predictions.
 *
 * Positions are read from the interpolation buffer every frame, never from React state.
 * Snapshots arrive at 10 Hz; if a marker's position went through a re-render the motion
 * would be visibly steppy and the whole interface would feel cheap. So React owns what a
 * track *is* — its classification, its threat, its label — and `useFrame` owns where it is.
 *
 * Threat is never carried by colour alone. Level drives marker scale, ring size, pulse
 * rate, trail opacity and prediction emphasis together, so the picture survives both a
 * colour-blind operator and a badly calibrated command-centre display.
 */

import { useMemo, useRef } from 'react';
import { useFrame } from '@react-three/fiber';
import { Html, Line } from '@react-three/drei';
import { AdditiveBlending, DoubleSide, type Group, type Mesh } from 'three';

import type { Track } from '@/types';
import { palette, threatColour, threatIntensity, threatLabel } from '@/lib/palette';
import { clamp } from '@/lib/geometry';
import { labelFor } from '@/simulation/threat';
import { interpolatedPosition } from '@/state/buffer';
import { useAetos } from '@/state/store';
import { LabelPriority } from '../labels/registry';
import { useLabel } from '../labels/useLabel';
import styles from './tracks.module.css';

export function Tracks() {
  const tracks = useAetos((s) => s.snapshot?.tracks ?? EMPTY);

  return (
    <group>
      {tracks.map((track) => (
        <TrackObject key={track.trackId} track={track} />
      ))}
    </group>
  );
}

const EMPTY: Track[] = [];

function TrackObject({ track }: { track: Track }) {
  /** Carries position only. */
  const marker = useRef<Group>(null);
  /** Carries the distance-normalised scale. Kept separate from `marker` so the label
   *  anchor below is not scaled along with the symbol and flung into orbit. */
  const symbol = useRef<Group>(null);
  /** Sits above the symbol; its offset tracks the symbol's on-screen size. */
  const labelAnchor = useRef<Group>(null);
  const halo = useRef<Mesh>(null);
  const shadow = useRef<Group>(null);
  const dropLine = useRef<Mesh>(null);

  const select = useAetos((s) => s.select);
  const selected = useAetos(
    (s) => s.selection?.kind === 'track' && s.selection.id === track.trackId,
  );
  const showTrajectories = useAetos((s) => s.layers.trajectories);
  const mode = useAetos((s) => s.mode);

  const colour = threatColour[track.threat.level];
  const intensity = threatIntensity[track.threat.level];

  /**
   * Track labels outrank every asset label. An asset is where it has always been and the
   * operator can find it again; a track is the thing that changed, and its rank follows
   * how much it warrants attention.
   */
  const label = useLabel(
    selected
      ? LabelPriority.SELECTED
      : track.threat.level === 'critical'
        ? LabelPriority.TRACK_CRITICAL
        : track.threat.level === 'high'
          ? LabelPriority.TRACK_HIGH
          : track.threat.level === 'medium'
            ? LabelPriority.TRACK_MEDIUM
            : LabelPriority.TRACK_LOW,
  );

  // Threat and selection add weight on top of the distance-normalised size, so severity
  // still reads as size rather than only as colour.
  const scale = 1 + intensity * 0.16 + (selected ? 0.28 : 0);
  const authorised = track.identity === 'authorised';

  /** Authorised traffic is drawn in its own colour: it is known, and not a threat. */
  const markerColour = authorised ? palette.authorised : colour;

  useFrame((state) => {
    const position = interpolatedPosition(track.trackId);
    if (!position) return;

    const x = position.e;
    const y = position.u;
    const z = -position.n;

    marker.current?.position.set(x, y, z);
    shadow.current?.position.set(x, 0.15, z);

    /**
     * Hold the symbol at a roughly constant size on screen.
     *
     * A marker sized in world metres is a two-pixel speck across the whole site and a
     * screen-filling blob when the camera closes on it — the two views where the operator
     * most needs it to be legible. Scaling with camera distance makes it a symbol rather
     * than an object, which is what it is: the system's mark for a thing far too small to
     * draw at true scale anyway.
     *
     * The constant is derived, not guessed. The halo's screen height is
     * 2·HALO_RADIUS·s / (2·d·tan(fov/2)); at a 42° vertical field of view that is
     * 16.95·s/d, so targeting ~3.5% of screen height gives s ≈ 0.002·d.
     */
    if (marker.current && symbol.current) {
      const distance = state.camera.position.distanceTo(marker.current.position);
      const normalised = clamp(distance * 0.002, 0.6, 9);
      symbol.current.scale.setScalar(normalised * scale);
      // Keep the label clear of the symbol at every zoom, without scaling the anchor.
      if (labelAnchor.current) {
        const offset = 6 + normalised * 11;
        labelAnchor.current.position.y = offset;
        // The declutter pass needs where this label *is*, and a track's anchor moves every
        // frame, so it is written here rather than once in an effect.
        label.world.set(x, y + offset, z);
      }
    }
    if (shadow.current) {
      const groundDistance = state.camera.position.distanceTo(shadow.current.position);
      shadow.current.scale.setScalar(clamp(groundDistance * 0.002, 0.6, 9));
    }

    // The drop line ties the object to the ground. Without it, altitude is unreadable in
    // a perspective view — everything looks like it is sitting on the terrain.
    if (dropLine.current) {
      dropLine.current.position.set(x, y / 2, z);
      dropLine.current.scale.set(1, Math.max(y, 0.001), 1);
    }

    if (halo.current) {
      const t = performance.now() * 0.001;
      const rate = 1 + intensity * 1.3;
      const pulse = 1 + Math.sin(t * rate) * (0.06 + intensity * 0.05);
      halo.current.scale.setScalar(pulse * (selected ? 1.45 : 1));
      const material = halo.current.material as { opacity: number };
      material.opacity = (0.2 + intensity * 0.13) * (selected ? 1.5 : 1);
    }
  });

  const historyPoints = useMemo(
    () =>
      track.history.map((p) => [p.position.e, p.position.u, -p.position.n] as [number, number, number]),
    [track.history],
  );

  const predictedPoints = useMemo(
    () => [
      [track.position.e, track.position.u, -track.position.n] as [number, number, number],
      ...track.predicted.map(
        (p) => [p.position.e, p.position.u, -p.position.n] as [number, number, number],
      ),
    ],
    [track.position, track.predicted],
  );

  const dim = mode === 'threat' && track.threat.level === 'low' && !selected;

  return (
    <group>
      {/* --- Marker ------------------------------------------------------- */}
      <group ref={marker}>
        <group
          ref={symbol}
          onClick={(event) => {
            event.stopPropagation();
            select({ kind: 'track', id: track.trackId });
          }}
          onPointerOver={(e) => {
            e.stopPropagation();
            document.body.style.cursor = 'pointer';
          }}
          onPointerOut={() => (document.body.style.cursor = 'default')}
        >
          {/* Core: an octahedron reads as an air object without pretending to be an
              airframe the sensors cannot actually resolve. */}
          <mesh>
            <octahedronGeometry args={[3.2, 0]} />
            <meshBasicMaterial color={markerColour} />
          </mesh>

          {/* Halo. Carries the pulse. */}
          <mesh ref={halo}>
            <sphereGeometry args={[6.5, 16, 12]} />
            <meshBasicMaterial
              color={markerColour}
              transparent
              opacity={0.25}
              blending={AdditiveBlending}
              depthWrite={false}
            />
          </mesh>

          {/* Attitude ring, edge-on to the ground plane. */}
          <mesh rotation={[-Math.PI / 2, 0, 0]}>
            <ringGeometry args={[7.5, 8.4, 32]} />
            <meshBasicMaterial
              color={markerColour}
              transparent
              opacity={0.5}
              blending={AdditiveBlending}
              depthWrite={false}
              side={DoubleSide}
            />
          </mesh>
        </group>

        {!dim && (
          <group ref={labelAnchor}>
          <Html
            position={[0, 0, 0]}
            center
            distanceFactor={undefined}
            zIndexRange={[20, 0]}
            style={{ pointerEvents: 'none' }}
          >
            <div
              ref={label.setElement}
              className={`${styles.label} ${selected ? styles.labelSelected : ''}`}
              style={{ borderColor: markerColour, color: markerColour }}
            >
              <span className={styles.labelId}>{track.trackId}</span>
              <span className={styles.labelMeta}>
                {labelFor(track.classification)} · {Math.round(track.altitude)} m
              </span>
              {!authorised && track.threat.level !== 'low' && (
                <span className={styles.labelThreat}>
                  {threatLabel[track.threat.level]} {track.threat.score}
                </span>
              )}
            </div>
          </Html>
          </group>
        )}
      </group>

      {/* --- Drop line to the ground -------------------------------------- */}
      <mesh ref={dropLine}>
        <cylinderGeometry args={[0.22, 0.22, 1, 4]} />
        <meshBasicMaterial
          color={markerColour}
          transparent
          opacity={dim ? 0.08 : 0.22}
          blending={AdditiveBlending}
          depthWrite={false}
        />
      </mesh>

      {/* --- Ground position ---------------------------------------------- */}
      <group ref={shadow}>
        <mesh rotation={[-Math.PI / 2, 0, 0]}>
          <ringGeometry args={[3.5, 4.4, 28]} />
          <meshBasicMaterial
            color={markerColour}
            transparent
            opacity={dim ? 0.15 : 0.45}
            blending={AdditiveBlending}
            depthWrite={false}
            side={DoubleSide}
          />
        </mesh>
      </group>

      {/* --- Trails -------------------------------------------------------- */}
      {showTrajectories && historyPoints.length > 1 && (
        <Line
          points={historyPoints}
          color={markerColour}
          lineWidth={selected ? 2 : 1.2}
          transparent
          opacity={dim ? 0.12 : 0.28 + intensity * 0.08}
        />
      )}

      {showTrajectories && predictedPoints.length > 1 && (
        <Line
          points={predictedPoints}
          color={markerColour}
          lineWidth={selected ? 2.4 : 1.6}
          transparent
          // The prediction is the load-bearing line — it is what makes the picture
          // asset-aware — so it is drawn brighter than the history, and dashed, because
          // a solid line would claim a certainty the extrapolation does not have.
          opacity={dim ? 0.15 : 0.5 + intensity * 0.15}
          dashed
          dashSize={12}
          gapSize={9}
        />
      )}
    </group>
  );
}
