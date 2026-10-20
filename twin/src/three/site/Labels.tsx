'use client';

/**
 * Labels for assets and sensors.
 *
 * Restrained on purpose, in two stages. First a candidate test: only assets carrying real
 * criticality, and large enough to point at cleanly, are named at rest; sensors are named
 * only in the system view where the operator is actually reading them. Then the shared
 * declutter pass in `three/labels/registry.ts` resolves whatever still overlaps on screen,
 * which is the part the candidate test cannot do because it depends on where the camera is.
 *
 * Every label answers at least one of the brief's eight questions — what is it, how well
 * protected is it, is the system itself healthy — and nothing is shown that answers none.
 */

import { useEffect } from 'react';
import { Html } from '@react-three/drei';

import type { Asset, Sensor } from '@/types';
import { palette, sensorColour, threatColour } from '@/lib/palette';
import { useAetos, useLiveSensors } from '@/state/store';
import { toWorld } from '../frame';
import { useLabel } from '../labels/useLabel';
import { LabelPriority } from '../labels/registry';
import styles from './labels.module.css';

export function Labels() {
  const visible = useAetos((s) => s.layers.labels);
  const mode = useAetos((s) => s.mode);
  const site = useAetos((s) => s.site);
  const sensors = useLiveSensors();

  if (!visible) return null;

  /**
   * Two tests, and an asset must pass both.
   *
   * Criticality earns a label. Footprint keeps it pointable: the transformers are 9/10 but
   * only 12 m across and sit inside the substation compound, so a label on each says almost
   * nothing the substation's own label does not, at the cost of three overlapping boxes.
   * An asset too small to point at cleanly is named when the operator selects it.
   */
  const labelledAssets = site.assets.filter((asset) => {
    const area = asset.footprint.width * asset.footprint.depth;
    return asset.criticality >= 7 && area >= 300;
  });

  return (
    <group>
      {labelledAssets.map((asset) => (
        <AssetLabel key={asset.id} asset={asset} />
      ))}

      {mode === 'system' &&
        sensors.map((sensor) => <SensorLabel key={sensor.id} sensor={sensor} />)}
    </group>
  );
}

function AssetLabel({ asset }: { asset: Asset }) {
  const select = useAetos((s) => s.select);
  const selected = useAetos((s) => s.selection?.kind === 'asset' && s.selection.id === asset.id);
  const live = useAetos((s) => s.snapshot?.assets[asset.id]);

  const threatened = (live?.activeThreats.length ?? 0) > 0;
  const protection = live?.protectionScore ?? asset.protectionScore;

  // Criticality sets the resting rank, so a 10/10 substation keeps its label where a 7/10
  // battery compound loses one. Coming under threat promotes it above every asset at rest.
  const priority = selected
    ? LabelPriority.SELECTED
    : (threatened ? LabelPriority.ASSET_THREATENED : LabelPriority.ASSET_BASE) +
      asset.criticality * 12;

  const label = useLabel(priority);
  const anchor = toWorld({ ...asset.position, u: asset.footprint.height + 8 });

  useEffect(() => {
    // A fixed asset's anchor never moves, so it is written once rather than every frame.
    label.world.set(anchor[0], anchor[1], anchor[2]);
  }, [label, anchor[0], anchor[1], anchor[2]]);

  return (
    <Html position={anchor} center zIndexRange={[15, 0]}>
      <button
        type="button"
        ref={label.setElement}
        className={`${styles.label} ${selected ? styles.selected : ''}`}
        style={{ borderColor: threatened ? threatColour.high : palette.holoDim }}
        onClick={(e) => {
          e.stopPropagation();
          select({ kind: 'asset', id: asset.id });
        }}
      >
        <span className={styles.name}>{asset.name}</span>
        <span className={styles.meta}>
          <span>CRIT {asset.criticality}/10</span>
          <span
            className={styles.protection}
            style={{
              color:
                protection > 0.85
                  ? palette.nominal
                  : protection > 0.6
                    ? palette.caution
                    : palette.warning,
            }}
          >
            PROT {Math.round(protection * 100)}%
          </span>
        </span>
      </button>
    </Html>
  );
}

function SensorLabel({ sensor }: { sensor: Sensor }) {
  const select = useAetos((s) => s.select);
  const selected = useAetos((s) => s.selection?.kind === 'sensor' && s.selection.id === sensor.id);

  // A failed sensor is the thing the operator opened the system view to find, so it
  // outranks every healthy one and every asset sitting quietly nearby.
  const priority = selected
    ? LabelPriority.SELECTED
    : sensor.status === 'offline'
      ? LabelPriority.SENSOR_OFFLINE
      : sensor.status === 'degraded'
        ? LabelPriority.SENSOR_DEGRADED
        : LabelPriority.SENSOR_NOMINAL;

  const label = useLabel(priority);
  const anchor = toWorld({ ...sensor.position, u: sensor.mastHeight + 6 });

  useEffect(() => {
    label.world.set(anchor[0], anchor[1], anchor[2]);
  }, [label, anchor[0], anchor[1], anchor[2]]);

  return (
    <Html position={anchor} center zIndexRange={[15, 0]}>
      <button
        type="button"
        ref={label.setElement}
        className={`${styles.label} ${styles.sensor} ${selected ? styles.selected : ''}`}
        style={{ borderColor: sensorColour[sensor.status] }}
        onClick={(e) => {
          e.stopPropagation();
          select({ kind: 'sensor', id: sensor.id });
        }}
      >
        <span className={styles.name}>{sensor.id}</span>
        <span className={styles.meta}>
          <span style={{ color: sensorColour[sensor.status] }}>
            {sensor.status.toUpperCase()}
          </span>
          <span>{Math.round(sensor.effectiveness * 100)}%</span>
        </span>
      </button>
    </Html>
  );
}
