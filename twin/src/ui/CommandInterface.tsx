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

import { DEMO_SITE } from '@/site/demo-site';
import { DEFAULT_SCENARIO_ID } from '@/simulation/scenarios';
import { attachSource } from '@/state/store';
import { SimulationSource } from '@/state/source';
import { Stage } from '@/three/Stage';
import { BottomBar } from './BottomBar';
import { ContextPanel } from './ContextPanel';
import { ThreatSummary } from './ThreatSummary';
import { TopBar } from './TopBar';
import styles from './interface.module.css';

export function CommandInterface() {
  useEffect(() => {
    // Swap this line for `new WebSocketSource(url)` when Aetos Core publishes the feed.
    // Nothing else in the application changes.
    return attachSource(new SimulationSource(DEMO_SITE, DEFAULT_SCENARIO_ID));
  }, []);

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
