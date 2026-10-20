'use client';

/**
 * Top bar: identity, site, and the state of the system itself.
 *
 * The readiness figure is paired with its weakness in the same glance. A bare "94%" is the
 * exact failure the brief calls out — it tells an operator nothing they can act on, so the
 * sentence next to it is the part that matters and it is given equal weight.
 */

import { palette } from '@/lib/palette';
import { useAetos, useThreatCounts } from '@/state/store';
import { LoadSiteControl } from './LoadSiteControl';
import styles from './topbar.module.css';

export function TopBar() {
  const site = useAetos((s) => s.site);
  const health = useAetos((s) => s.snapshot?.health ?? null);
  const weather = useAetos((s) => s.snapshot?.weather ?? null);
  const connected = useAetos((s) => s.connected);
  const counts = useThreatCounts();

  const readiness = health ? Math.round(health.readiness * 100) : null;
  const readinessColour =
    readiness === null
      ? palette.textFaint
      : readiness >= 85
        ? palette.nominal
        : readiness >= 65
          ? palette.caution
          : palette.warning;

  return (
    <header className={`${styles.bar} panel`}>
      <div className={styles.identity}>
        <div className={styles.wordmark}>
          AETOS<span className={styles.wordmarkThin}> COMMAND</span>
        </div>
        <div className={styles.site}>
          <span className={styles.siteId}>{site.id}</span>
          <span className={styles.siteName}>{site.name}</span>
        </div>
        <LoadSiteControl />
      </div>

      <div className={styles.metrics}>
        <Metric label="Airspace" value={`${counts.total}`} unit="tracks" />
        <Metric
          label="Threats"
          value={`${counts.critical + counts.high}`}
          unit="elevated"
          colour={counts.critical > 0 ? palette.danger : counts.high > 0 ? palette.warning : undefined}
        />
        <Metric label="Conditions" value={weather?.label ?? '—'} />
        <Metric
          label="Feed"
          value={connected ? 'LIVE' : 'WAITING'}
          colour={connected ? palette.nominal : palette.caution}
        />
      </div>

      <div className={styles.readiness}>
        <div className={styles.readinessFigure}>
          <span className="heading">Aetos readiness</span>
          <span className={`${styles.readinessValue} value`} style={{ color: readinessColour }}>
            {readiness === null ? '—' : `${readiness}%`}
          </span>
        </div>

        <div className={styles.diagnosis}>
          {health?.primaryWeakness ? (
            <>
              <span className={styles.weakness}>{health.primaryWeakness}</span>
              {health.recommendedAction && (
                <span className={styles.action}>{health.recommendedAction}</span>
              )}
            </>
          ) : (
            <span className={styles.nominal}>No degradation detected.</span>
          )}
        </div>
      </div>
    </header>
  );
}

function Metric({
  label,
  value,
  unit,
  colour,
}: {
  label: string;
  value: string;
  unit?: string;
  colour?: string;
}) {
  return (
    <div className={styles.metric}>
      <span className="heading">{label}</span>
      <span className={`${styles.metricValue} value`} style={colour ? { color: colour } : undefined}>
        {value}
        {unit && <span className={styles.metricUnit}>{unit}</span>}
      </span>
    </div>
  );
}
