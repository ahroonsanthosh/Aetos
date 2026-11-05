'use client';

/**
 * Bottom bar: modes, layers and the simulation.
 *
 * The simulation controls are grouped and labelled as simulation, separately from the
 * operational controls, because a demonstration interface that blurs the two teaches
 * people the wrong thing about what the system is doing.
 */

import { palette } from '@/lib/palette';
import { SCENARIOS } from '@/simulation/scenarios';
import type { LayerToggles } from '@/state/store';
import type { ViewMode } from '@/types';
import { useAetos } from '@/state/store';
import styles from './bottom.module.css';

const MODES: { id: ViewMode; label: string; hint: string }[] = [
  { id: 'overview', label: 'Overview', hint: 'Whole site, everything visible' },
  { id: 'tactical', label: 'Tactical', hint: 'Closer, tracks and zones emphasised' },
  { id: 'threat', label: 'Threat', hint: 'Non-critical plant recedes' },
  { id: 'system', label: 'System', hint: 'Sensors, coverage and health' },
];

const LAYERS: { id: keyof LayerToggles; label: string }[] = [
  { id: 'zones', label: 'Zones' },
  { id: 'sensorCoverage', label: 'Sensor coverage' },
  { id: 'coverageMap', label: 'Coverage map' },
  { id: 'trajectories', label: 'Trajectories' },
  { id: 'labels', label: 'Labels' },
];

export function BottomBar() {
  const mode = useAetos((s) => s.mode);
  const setMode = useAetos((s) => s.setMode);
  const layers = useAetos((s) => s.layers);
  const toggleLayer = useAetos((s) => s.toggleLayer);
  const scenarioId = useAetos((s) => s.scenarioId);
  const setScenario = useAetos((s) => s.setScenario);
  const paused = useAetos((s) => s.paused);
  const setPaused = useAetos((s) => s.setPaused);
  const rate = useAetos((s) => s.rate);
  const setRate = useAetos((s) => s.setRate);

  const scenario = SCENARIOS.find((s) => s.id === scenarioId);
  const feed = useAetos((s) => s.feed);
  const setFeed = useAetos((s) => s.setFeed);
  const liveStatus = useAetos((s) => s.liveStatus);
  const liveFallback = useAetos((s) => s.liveFallback);

  return (
    <footer className={`${styles.bar} panel`}>
      <div className={styles.group}>
        <span className="heading">View</span>
        <div className={styles.buttons}>
          {MODES.map((m) => (
            <button
              key={m.id}
              type="button"
              title={m.hint}
              onClick={() => setMode(m.id)}
              className={`${styles.button} ${mode === m.id ? styles.buttonActive : ''}`}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      <div className={styles.group}>
        <span className="heading">Layers</span>
        <div className={styles.buttons}>
          {LAYERS.map((layer) => (
            <button
              key={layer.id}
              type="button"
              onClick={() => toggleLayer(layer.id)}
              className={`${styles.toggle} ${layers[layer.id] ? styles.toggleOn : ''}`}
            >
              <span
                className={styles.dot}
                style={{ background: layers[layer.id] ? palette.holo : 'transparent' }}
              />
              {layer.label}
            </button>
          ))}
        </div>
      </div>

      <div className={`${styles.group} ${styles.simulation}`}>
        <span className="heading">
          Feed
          {feed === 'simulation' && <span className={styles.simTag}>synthetic data</span>}
        </span>

        <div className={styles.simRow}>
          <div className={styles.buttons} role="group" aria-label="Feed">
            <button
              type="button"
              onClick={() => setFeed('live')}
              className={`${styles.button} ${feed === 'live' ? styles.buttonActive : ''}`}
              title="The Aetos console's own site and tracks"
            >
              Live
            </button>
            <button
              type="button"
              onClick={() => setFeed('simulation')}
              className={`${styles.button} ${feed === 'simulation' ? styles.buttonActive : ''}`}
              title="Built-in scenarios on the demo or a loaded site"
            >
              Simulation
            </button>
          </div>

          {feed === 'simulation' && (
            <>
              <select
                className={styles.select}
                value={scenarioId}
                onChange={(event) => setScenario(event.target.value)}
              >
                {SCENARIOS.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>

              <button
                type="button"
                className={styles.button}
                onClick={() => setPaused(!paused)}
                title={paused ? 'Resume' : 'Pause'}
              >
                {paused ? 'Resume' : 'Pause'}
              </button>

              <div className={styles.rates}>
                {[0.5, 1, 2, 4].map((r) => (
                  <button
                    key={r}
                    type="button"
                    onClick={() => setRate(r)}
                    className={`${styles.rate} ${rate === r ? styles.buttonActive : ''}`}
                  >
                    {r}×
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        {feed === 'live' && (
          <p className={styles.scenarioNote}>
            {liveStatus?.state === 'interrupted'
              ? `Feed interrupted: ${liveStatus.reason} Retrying every second.`
              : liveStatus?.state === 'connecting'
                ? 'Connecting to the Aetos console…'
                : 'Tracks from the Aetos console, updated every second. Risk scores, reasons and recommendations are Aetos’s own.'}
          </p>
        )}
        {feed === 'simulation' && liveFallback && (
          <p className={styles.scenarioNote}>Live feed unavailable: {liveFallback}</p>
        )}
        {feed === 'simulation' && !liveFallback && scenario && (
          <p className={styles.scenarioNote}>{scenario.description}</p>
        )}
      </div>
    </footer>
  );
}
