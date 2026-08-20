'use client';

/**
 * Labels for assets and sensors.
 *
 * Restrained on purpose. Labelling everything is the fastest way to turn a digital twin
 * into a cluttered map, so only assets that carry real criticality are named at rest, and
 * sensors are named only in the system view where the operator is actually reading them.
 *
 * Every label answers at least one of the brief's eight questions — what is it, how well
 * protected is it, is the system itself healthy — and nothing is shown that answers none.
 */

import { Html } from '@react-three/drei';

import { palette, sensorColour, threatColour } from '@/lib/palette';
import { useAetos, useLiveSensors } from '@/state/store';
import { toWorld } from '../frame';
import styles from './labels.module.css';

export function Labels() {
  const visible = useAetos((s) => s.layers.labels);
  const mode = useAetos((s) => s.mode);
  const site = useAetos((s) => s.site);
  const assetState = useAetos((s) => s.snapshot?.assets);
  const select = useAetos((s) => s.select);
  const selection = useAetos((s) => s.selection);
  const sensors = useLiveSensors();

  if (!visible) return null;

  /**
   * Two tests, and an asset must pass both.
   *
   * Criticality earns a label. Footprint keeps it readable: the transformers are 9/10 but
   * only 12 m across and sit inside the substation compound, so their labels landed on top
   * of the substation's and of each other. An asset too small to point at cleanly is named
   * when the operator selects it, not before — the alternative is that the label they
   * needed is lost among the ones they did not.
   */
  const labelledAssets = site.assets.filter((asset) => {
    const area = asset.footprint.width * asset.footprint.depth;
    const selected = selection?.kind === 'asset' && selection.id === asset.id;
    return selected || (asset.criticality >= 7 && area >= 300);
  });

  return (
    <group>
      {labelledAssets.map((asset) => {
        const live = assetState?.[asset.id];
        const threatened = (live?.activeThreats.length ?? 0) > 0;
        const protection = live?.protectionScore ?? asset.protectionScore;
        const selected = selection?.kind === 'asset' && selection.id === asset.id;

        return (
          <Html
            key={asset.id}
            position={toWorld({ ...asset.position, u: asset.footprint.height + 8 })}
            center
            zIndexRange={[15, 0]}
          >
            <button
              type="button"
              className={`${styles.label} ${selected ? styles.selected : ''}`}
              style={{
                borderColor: threatened ? threatColour.high : palette.holoDim,
              }}
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
      })}

      {mode === 'system' &&
        sensors.map((sensor) => {
          const selected = selection?.kind === 'sensor' && selection.id === sensor.id;
          return (
            <Html
              key={sensor.id}
              position={toWorld({ ...sensor.position, u: sensor.mastHeight + 6 })}
              center
              zIndexRange={[15, 0]}
            >
              <button
                type="button"
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
        })}
    </group>
  );
}
