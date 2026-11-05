'use client';

/**
 * Aetos Command — the whole interface.
 *
 * Layout is deliberately edge-anchored: the hologram occupies the entire viewport and the
 * panels sit over its corners. The brief's instruction is that the 3D scene is the primary
 * interface and the UI exists around it, so nothing here is allowed to become a dashboard
 * that the scene is embedded in.
 *
 * This component also owns the data source lifecycle: `SimulationSource` for the built-in
 * scenarios, `LiveSource` for the Aetos console's own tracks. Nothing else names either.
 */

import { useEffect } from 'react';

import { parseSiteFileText } from '@/site/schema';
import { takeHandedOffSite } from '@/site/handoff';
import { attachSource, useAetos } from '@/state/store';
import { SimulationSource } from '@/state/source';
import { LiveSource } from '@/live/LiveSource';
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
  const feed = useAetos((s) => s.feed);
  const setFeed = useAetos((s) => s.setFeed);

  // Once, on mount: pick up a site the site editor handed off just before navigating here.
  // Deliberately not keyed on anything — this is a one-shot pickup, not a poll — so it runs
  // exactly once regardless of how many times the site changes afterwards.
  // Once, on mount: pick up a site the site editor handed off just before navigating here
  // (which means the simulation), otherwise decide the feed. Live when this page is served by
  // Aetos at /twin/, simulation when the twin runs on its own.
  useEffect(() => {
    const handoff = takeHandedOffSite();
    if (handoff) {
      try {
        setSite(parseSiteFileText(handoff));
        return;
      } catch {
        // A malformed handoff is dropped; the feed is decided as if there were none.
      }
    }
    setFeed(window.location.pathname.startsWith('/twin') ? 'live' : 'simulation');
  }, [setSite, setFeed]);

  // The simulation source, for whichever site the simulation is on. Seeded from the store's
  // `scenarioId`, not the module default: `setSite` resets that to 'idle'. Switching scenario is
  // handled by the running source via `loadScenario`, so `scenarioId` is deliberately absent.
  useEffect(() => {
    if (feed !== 'simulation') return;
    return attachSource(new SimulationSource(site, scenarioId));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site, feed]);

  // The live source. It sets the site itself (the real Aetos site), which must not restart it,
  // so it depends on the feed alone.
  useEffect(() => {
    if (feed !== 'live') return;
    const { setLiveSite, setLiveStatus } = useAetos.getState();
    return attachSource(new LiveSource({ onSite: setLiveSite, onStatus: setLiveStatus }));
  }, [feed]);

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
