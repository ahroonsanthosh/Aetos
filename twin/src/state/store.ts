/**
 * Aetos state.
 *
 * One store, consumed identically by the 3D scene and the DOM interface — the brief's
 * architectural requirement. A component never reaches past this into the simulator, and
 * no component creates situational data of its own.
 *
 * High-frequency track positions deliberately do NOT live here; they live in `buffer.ts`
 * and are read per-frame. What lives here is the state an operator reads: what is
 * selected, what mode we are in, and the latest assessed picture.
 */

import { useMemo } from 'react';
import { create } from 'zustand';
import { shallow } from 'zustand/shallow';
import { useShallow } from 'zustand/react/shallow';

import type {
  Selection,
  Sensor,
  Site,
  Snapshot,
  ThreatLevel,
  Track,
  ViewMode,
  Zone,
} from '@/types';
import { DEMO_SITE } from '@/site/demo-site';
import { DEFAULT_SCENARIO_ID } from '@/simulation/scenarios';
import type { DataSource } from './source';
import { pushSnapshot, resetBuffer } from './buffer';

export interface LayerToggles {
  /** Sensor positions and coverage volumes. */
  sensorCoverage: boolean;
  /** The site coverage grid, answering "where am I vulnerable?". */
  coverageMap: boolean;
  /** Protected zone volumes. */
  zones: boolean;
  /** Floating labels on assets and sensors. */
  labels: boolean;
  /** Track history and prediction lines. */
  trajectories: boolean;
}

interface AetosState {
  site: Site;
  snapshot: Snapshot | null;
  mode: ViewMode;
  selection: Selection | null;
  scenarioId: string;
  paused: boolean;
  rate: number;
  layers: LayerToggles;
  /** Set once the source has delivered its first snapshot. */
  connected: boolean;

  setSnapshot(snapshot: Snapshot): void;
  setMode(mode: ViewMode): void;
  select(selection: Selection | null): void;
  setScenario(scenarioId: string): void;
  setPaused(paused: boolean): void;
  setRate(rate: number): void;
  toggleLayer(layer: keyof LayerToggles): void;
}

/**
 * The active source. Held outside the store because it is a live object with a socket or
 * a timer, not state — putting it in the store would make every consumer a subscriber to
 * something none of them should re-render for.
 */
let source: DataSource | null = null;

/** Layer defaults per mode. Changing mode changes what the environment emphasises. */
const MODE_LAYERS: Record<ViewMode, Partial<LayerToggles>> = {
  overview: { sensorCoverage: false, coverageMap: false, zones: true, labels: true, trajectories: true },
  tactical: { sensorCoverage: false, coverageMap: false, zones: true, labels: true, trajectories: true },
  threat: { sensorCoverage: false, coverageMap: false, zones: true, labels: false, trajectories: true },
  system: { sensorCoverage: true, coverageMap: true, zones: false, labels: true, trajectories: false },
};

export const useAetos = create<AetosState>((set, get) => ({
  site: DEMO_SITE,
  snapshot: null,
  mode: 'overview',
  selection: null,
  scenarioId: DEFAULT_SCENARIO_ID,
  paused: false,
  rate: 1,
  connected: false,
  layers: {
    sensorCoverage: false,
    coverageMap: false,
    zones: true,
    labels: true,
    trajectories: true,
  },

  setSnapshot(snapshot) {
    pushSnapshot(snapshot);

    const state = get();
    // Drop a selection whose subject has left the picture, so the context panel can never
    // describe a track that no longer exists.
    const selection =
      state.selection?.kind === 'track' &&
      !snapshot.tracks.some((t) => t.trackId === state.selection!.id)
        ? null
        : state.selection;

    set({ snapshot, connected: true, selection });
  },

  setMode(mode) {
    set((state) => ({ mode, layers: { ...state.layers, ...MODE_LAYERS[mode] } }));
  },

  select(selection) {
    set({ selection });
  },

  setScenario(scenarioId) {
    resetBuffer();
    source?.loadScenario?.(scenarioId);
    set({ scenarioId, selection: null });
  },

  setPaused(paused) {
    source?.setPaused?.(paused);
    set({ paused });
  },

  setRate(rate) {
    source?.setRate?.(rate);
    set({ rate });
  },

  toggleLayer(layer) {
    set((state) => ({ layers: { ...state.layers, [layer]: !state.layers[layer] } }));
  },
}));

// ---------------------------------------------------------------------------
// Source lifecycle
// ---------------------------------------------------------------------------

export function attachSource(next: DataSource): () => void {
  source?.stop();
  resetBuffer();
  source = next;
  next.start((snapshot) => useAetos.getState().setSnapshot(snapshot));

  return () => {
    next.stop();
    if (source === next) source = null;
    resetBuffer();
  };
}

export function sourceKind(): DataSource['kind'] | null {
  return source?.kind ?? null;
}

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

const LEVEL_ORDER: Record<ThreatLevel, number> = { critical: 3, high: 2, medium: 1, low: 0 };

/** Tracks sorted by how much they warrant attention. */
export function useRankedTracks(): Track[] {
  return useAetos(
    useShallow((s) =>
      [...(s.snapshot?.tracks ?? [])].sort(
        (a, b) =>
          LEVEL_ORDER[b.threat.level] - LEVEL_ORDER[a.threat.level] ||
          b.threat.score - a.threat.score,
      ),
    ),
  );
}

export interface ThreatCounts {
  critical: number;
  high: number;
  medium: number;
  low: number;
  total: number;
}

export function useThreatCounts(): ThreatCounts {
  return useAetos(
    useShallow((s) => {
      const counts: ThreatCounts = { critical: 0, high: 0, medium: 0, low: 0, total: 0 };
      for (const track of s.snapshot?.tracks ?? []) {
        counts[track.threat.level] += 1;
        counts.total += 1;
      }
      return counts;
    }),
  );
}

export function useSelectedTrack(): Track | null {
  return useAetos((s) =>
    s.selection?.kind === 'track'
      ? (s.snapshot?.tracks.find((t) => t.trackId === s.selection!.id) ?? null)
      : null,
  );
}

/** The asset the operator has selected, merged with its live state. */
export function useSelectedAsset() {
  return useAetos(
    useShallow((s) => {
      if (s.selection?.kind !== 'asset') return null;
      const asset = s.site.assets.find((a) => a.id === s.selection!.id);
      if (!asset) return null;
      const live = s.snapshot?.assets[asset.id];
      return live ? { ...asset, ...live } : asset;
    }),
  );
}

export function useSelectedSensor() {
  return useAetos(
    useShallow((s) => {
      if (s.selection?.kind !== 'sensor') return null;
      const sensor = s.site.sensors.find((x) => x.id === s.selection!.id);
      if (!sensor) return null;
      const live = s.snapshot?.sensors[sensor.id];
      return live ? { ...sensor, ...live } : sensor;
    }),
  );
}

/**
 * Sensors merged with their live state, for the system view.
 *
 * Merged in `useMemo` rather than inside the selector. A selector that builds fresh
 * objects can never satisfy `useShallow` — the array elements compare by reference, a new
 * object is never reference-equal to the last one, and `useSyncExternalStore` responds to
 * a snapshot that changes on every read by re-rendering forever. Selecting the two stable
 * references and combining them outside the selector is the fix, and it applies to every
 * derived list in this file.
 */
export function useLiveSensors(): Sensor[] {
  const sensors = useAetos((s) => s.site.sensors);
  const live = useAetos((s) => s.snapshot?.sensors);

  return useMemo(
    () =>
      sensors.map((sensor) => {
        const state = live?.[sensor.id];
        return state ? { ...sensor, ...state } : sensor;
      }),
    [sensors, live],
  );
}

/** Zones merged with their live emphasis. Same construction, same reason. */
export function useLiveZones(): Zone[] {
  const zones = useAetos((s) => s.site.zones);
  const live = useAetos((s) => s.snapshot?.zones);

  return useMemo(
    () =>
      zones.map((zone) => {
        const state = live?.[zone.id];
        return state ? { ...zone, ...state } : zone;
      }),
    [zones, live],
  );
}

export { shallow };
