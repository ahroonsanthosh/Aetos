'use client';

/**
 * Aetos Command — the whole interface.
 *
 * Layout is deliberately edge-anchored: the hologram occupies the entire viewport and the
 * panels sit over its corners. The brief's instruction is that the 3D scene is the primary
 * interface and the UI exists around it, so nothing here is allowed to become a dashboard
 * that the scene is embedded in.
 *
 * This component also owns the data source lifecycle. It is the only place in the
 * application that names `SimulationSource`, which is what makes swapping it for
 * `WebSocketSource` a one-line change.
 */

import { useEffect } from 'react';

import { parseSiteFileText } from '@/site/schema';
import { takeHandedOffSite } from '@/site/handoff';
import { attachSource, useAetos } from '@/state/store';
import { SimulationSource } from '@/state/source';
import { Stage } from '@/three/Stage';
import { BottomBar } from './BottomBar';
import { ContextPanel } from './ContextPanel';
import { ThreatSummary } from './ThreatSummary';
import { TopBar } from './TopBar';
import styles from './interface.module.css';

export function CommandInterface() {
  const site = useAetos((s) => s.site);
  const setSite = useAetos((s) => s.setSite);
  const scenarioId = useAetos((s) => s.scenarioId);

  // Once, on mount: pick up a site the site editor handed off just before navigating here.
  // Deliberately not keyed on anything — this is a one-shot pickup, not a poll — so it runs
  // exactly once regardless of how many times the site changes afterwards.
  useEffect(() => {
    const handoff = takeHandedOffSite();
    if (!handoff) return;
    try {
      setSite(parseSiteFileText(handoff));
    } catch {
      // A malformed handoff is dropped silently rather than surfaced as an error before
      // the interface has even rendered — the demo site it falls back to is still a
      // working command view, which is the better failure mode here.
    }
  }, [setSite]);

  useEffect(() => {
    // Swap this line for `new WebSocketSource(url)` when Aetos Core publishes the feed.
    // Nothing else in the application changes. Re-runs whenever `site` changes — a fresh
    // simulation source for whichever site is currently loaded, demo or otherwise.
    //
    // Seeded from the store's `scenarioId`, not the module default: `setSite` resets that
    // to 'idle', and constructing the engine with the default instead would leave the
    // scenario control reading "Reset" while a UAV ran a scripted approach behind it —
    // a command display disagreeing with itself about what it is showing.
    return attachSource(new SimulationSource(site, scenarioId));
    // `scenarioId` is deliberately absent: switching scenario is handled by the running
    // source via `loadScenario`, and rebuilding the whole engine for it would discard
    // track history and restart the clock on every dropdown change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site]);

  return (
    <main className={styles.root}>
      <div className={styles.stage}>
        <Stage />
      </div>

      <div className={styles.overlay}>
        <TopBar />
        <div className={styles.middle}>
          <ThreatSummary />
          <ContextPanel />
        </div>
        <BottomBar />
      </div>
    </main>
  );
}
