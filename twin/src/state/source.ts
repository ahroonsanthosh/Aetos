/**
 * Where snapshots come from.
 *
 * This interface is the whole point of the architecture. The scene and the interface
 * consume `Snapshot`s and never learn which implementation produced them. Replacing the
 * simulator with Aetos Core is a matter of constructing a different `DataSource` — no
 * component changes, no store changes, no shader changes.
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

// ---------------------------------------------------------------------------

/**
 * The live implementation, written now so the seam is real rather than asserted.
 *
 * Untested against a server — Aetos Core does not yet publish this feed. It is here to
 * keep `DataSource` honest: if the interface could not be satisfied by a socket, the
 * abstraction would be decoration.
 */
export class WebSocketSource implements DataSource {
  readonly kind = 'live' as const;

  private socket: WebSocket | null = null;
  private listener?: (snapshot: Snapshot) => void;
  private reconnectDelay = 1000;
  private closed = false;

  constructor(private readonly url: string) {}

  start(onSnapshot: (snapshot: Snapshot) => void): void {
    this.listener = onSnapshot;
    this.closed = false;
    this.connect();
  }

  stop(): void {
    this.closed = true;
    this.socket?.close();
    this.socket = null;
    this.listener = undefined;
  }

  private connect(): void {
    if (this.closed) return;
    const socket = new WebSocket(this.url);
    this.socket = socket;

    socket.onmessage = (event) => {
      try {
        this.listener?.(JSON.parse(event.data as string) as Snapshot);
      } catch {
        // A malformed frame is dropped rather than allowed to tear down the feed. The
        // sequence gap in the next good snapshot is what tells the interface it happened.
      }
    };

    socket.onopen = () => {
      this.reconnectDelay = 1000;
    };

    socket.onclose = () => {
      if (this.closed) return;
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 15000);
    };
  }
}
