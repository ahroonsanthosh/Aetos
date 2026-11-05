/**
 * Where snapshots come from.
 *
 * This interface is the whole point of the architecture. The scene and the interface
 * consume `Snapshot`s and never learn which implementation produced them: `SimulationSource`
 * here, or `LiveSource` (`src/live/`) for the Aetos console's own tracks.
 */

import type { Site, Snapshot } from '@/types';
import { SimulationEngine, TICK_MS } from '@/simulation/engine';

export type SourceKind = 'simulation' | 'live';

export interface DataSource {
  readonly kind: SourceKind;
  /** Begin publishing. Calling twice is a no-op. */
  start(onSnapshot: (snapshot: Snapshot) => void): void;
  stop(): void;
  /** Simulator-only controls. A live source leaves these undefined. */
  loadScenario?(scenarioId: string): void;
  setPaused?(paused: boolean): void;
  /** Time multiplier. 1 is real time. */
  setRate?(rate: number): void;
}

// ---------------------------------------------------------------------------

export class SimulationSource implements DataSource {
  readonly kind = 'simulation' as const;

  private readonly engine: SimulationEngine;
  private timer: ReturnType<typeof setInterval> | null = null;
  private paused = false;
  private rate = 1;
  private listener?: (snapshot: Snapshot) => void;

  constructor(site: Site, scenarioId?: string) {
    this.engine = new SimulationEngine(site, scenarioId);
  }

  start(onSnapshot: (snapshot: Snapshot) => void): void {
    this.listener = onSnapshot;
    if (this.timer) return;

    // Emit one snapshot immediately so the interface never renders an empty frame while
    // waiting for the first tick.
    this.emit(0);
    this.timer = setInterval(() => this.emit(TICK_MS / 1000), TICK_MS);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.listener = undefined;
  }

  loadScenario(scenarioId: string): void {
    this.engine.loadScenario(scenarioId);
    // Publish the new picture at once rather than after up to a tick of stale data.
    this.emit(0);
  }

  setPaused(paused: boolean): void {
    this.paused = paused;
  }

  setRate(rate: number): void {
    this.rate = rate;
  }

  private emit(dt: number): void {
    if (!this.listener) return;
    // A paused simulation still publishes. Freezing the feed entirely would make the
    // interface indistinguishable from a stalled data source, which is the one thing a
    // command display must never be ambiguous about.
    this.listener(this.engine.tick(this.paused ? 0 : dt * this.rate));
  }
}
