'use client';

/**
 * Right panel: everything about whatever is selected.
 *
 * One panel, four subjects. Which one is shown follows the selection, and when nothing is
 * selected it falls back to the picture the current mode is about — system health in
 * system mode, site protection otherwise. An empty panel would waste the most valuable
 * fixed real estate on the screen.
 *
 * The recommendation block is the last thing in the track view and says what it says: this
 * is advice to a human. There is no control here that does anything to the physical world,
 * and there is deliberately nowhere for one to go.
 */

import type { ReactNode } from 'react';

import { palette, threatColour, threatLabel } from '@/lib/palette';
import { labelFor } from '@/simulation/threat';
import type {
  Asset,
  RecommendationCategory,
  Sensor,
  SystemHealth,
  Track,
} from '@/types';
import {
  useAetos,
  useSelectedAsset,
  useSelectedSensor,
  useSelectedTrack,
} from '@/state/store';
import styles from './context.module.css';

export function ContextPanel() {
  const track = useSelectedTrack();
  const asset = useSelectedAsset();
  const sensor = useSelectedSensor();
  const mode = useAetos((s) => s.mode);
  const health = useAetos((s) => s.snapshot?.health ?? null);

  return (
    <aside className={`${styles.panel} panel`}>
      {track ? (
        <TrackView track={track} />
      ) : asset ? (
        <AssetView asset={asset} />
      ) : sensor ? (
        <SensorView sensor={sensor} />
      ) : mode === 'system' && health ? (
        <SystemView health={health} />
      ) : (
        <SiteView />
      )}
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Shared pieces
// ---------------------------------------------------------------------------

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className={styles.section}>
      <h3 className="heading">{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, value, colour }: { label: string; value: ReactNode; colour?: string }) {
  return (
    <div className={styles.row}>
      <span className={styles.rowLabel}>{label}</span>
      <span className={`${styles.rowValue} value`} style={colour ? { color: colour } : undefined}>
        {value}
      </span>
    </div>
  );
}

function Bar({ value, colour }: { value: number; colour: string }) {
  return (
    <div className={styles.bar}>
      <span
        className={styles.barFill}
        style={{ width: `${Math.round(Math.min(Math.max(value, 0), 1) * 100)}%`, background: colour }}
      />
    </div>
  );
}

function scoreColour(value: number): string {
  if (value >= 0.85) return palette.nominal;
  if (value >= 0.6) return palette.caution;
  if (value >= 0.35) return palette.warning;
  return palette.danger;
}

// ---------------------------------------------------------------------------
// Track
// ---------------------------------------------------------------------------

const RECOMMENDATION_TEXT: Record<RecommendationCategory, string> = {
  monitor: 'Monitor',
  verify: 'Verify identity',
  alert: 'Hold under alert',
  escalate: 'Escalate for review',
  operator_review_required: 'Operator review required',
};

function TrackView({ track }: { track: Track }) {
  const recommendation = useAetos((s) =>
    s.snapshot?.recommendations.find((r) => r.trackId === track.trackId),
  );
  const site = useAetos((s) => s.site);

  const authorised = track.identity === 'authorised';
  const colour = authorised ? palette.authorised : threatColour[track.threat.level];
  const threatenedAsset = track.threat.threatenedAssetId
    ? site.assets.find((a) => a.id === track.threat.threatenedAssetId)
    : undefined;

  return (
    <>
      <header className={styles.header} style={{ borderColor: colour }}>
        <div className={styles.headerTop}>
          <span className={styles.subject}>{track.trackId}</span>
          <span className={styles.kind} style={{ color: colour }}>
            {labelFor(track.classification)}
          </span>
        </div>
        <span className={styles.headerMeta}>
          {Math.round(track.confidence * 100)}% classification confidence ·{' '}
          {track.contributingSensors.length} sensor
          {track.contributingSensors.length === 1 ? '' : 's'}
        </span>
      </header>

      <Section title="Kinematics">
        <Row label="Speed" value={`${track.speed.toFixed(1)} m/s`} />
        <Row label="Altitude" value={`${Math.round(track.altitude)} m`} />
        <Row label="Range" value={`${Math.round(track.range)} m`} />
        <Row label="Heading" value={`${Math.round((track.heading * 180) / Math.PI)}°`} />
      </Section>

      <Section title="Assessment">
        <Row label="Behaviour" value={capitalise(track.behaviour)} />
        <Row
          label="Identity"
          value={authorised ? 'Authorised' : track.identity === 'known' ? 'Known' : 'Unknown'}
          colour={authorised ? palette.authorised : track.identity === 'unknown' ? palette.caution : undefined}
        />
        <Row label="Track status" value={capitalise(track.status)} />
        {threatenedAsset && (
          <Row label="Threatens" value={threatenedAsset.name} colour={colour} />
        )}
        {track.threat.etaToProtectedZone !== undefined && (
          <Row
            label="Zone entry"
            value={`${Math.round(track.threat.etaToProtectedZone)} s`}
            colour={colour}
          />
        )}
      </Section>

      <section className={styles.threatBlock} style={{ borderColor: colour }}>
        <div className={styles.threatTop}>
          <span className="heading">Threat</span>
          <span className={styles.threatLevel} style={{ color: colour }}>
            {authorised ? 'AUTHORISED' : threatLabel[track.threat.level]}
          </span>
        </div>
        <div className={styles.threatScore} style={{ color: colour }}>
          {track.threat.score}
          <span className={styles.threatScoreMax}>/ 100</span>
        </div>
        <Bar value={track.threat.score / 100} colour={colour} />
      </section>

      {/* Why the number is what it is. The panel must never show a score it cannot
          explain — that is the difference between an assessment and an oracle. */}
      <Section title="Contributing factors">
        <ul className={styles.factors}>
          {track.threat.factors.slice(0, 6).map((factor) => (
            <li key={factor.code} className={styles.factor}>
              <span className={styles.factorLabel}>{factor.label}</span>
              <span
                className={`${styles.factorValue} value`}
                style={{ color: factor.contribution < 0 ? palette.textFaint : colour }}
              >
                {factor.contribution > 0 ? '+' : ''}
                {factor.contribution}
              </span>
            </li>
          ))}
        </ul>
      </Section>

      {recommendation && (
        <section className={styles.recommendation}>
          <h3 className="heading">Recommended response</h3>
          <p className={styles.recommendationValue}>
            {RECOMMENDATION_TEXT[recommendation.category]}
          </p>
          <p className={styles.recommendationRationale}>{recommendation.rationale}</p>
          <p className={styles.advisory}>
            Advisory only. Aetos does not authorise or execute any response.
          </p>
        </section>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Asset
// ---------------------------------------------------------------------------

function AssetView({ asset }: { asset: Asset }) {
  const site = useAetos((s) => s.site);
  const snapshot = useAetos((s) => s.snapshot);

  const covering = snapshot
    ? site.sensors.filter((sensor) => {
        const live = snapshot.sensors[sensor.id];
        return live && live.status !== 'offline';
      })
    : [];

  const available = site.resources.filter((r) => r.status === 'available');
  const zone = site.zones.find((z) => z.id === asset.protectionZoneId);
  const threats = asset.activeThreats.length;

  return (
    <>
      <header className={styles.header} style={{ borderColor: threats ? palette.warning : palette.holo }}>
        <div className={styles.headerTop}>
          <span className={styles.subject}>{asset.name}</span>
          <span className={styles.kind}>{asset.criticality >= 8 ? 'CRITICAL ASSET' : 'ASSET'}</span>
        </div>
        <span className={styles.headerMeta}>{asset.id} · {capitalise(asset.type.replace('_', ' '))}</span>
      </header>

      <Section title="Status">
        <Row label="Criticality" value={`${asset.criticality} / 10`} />
        <Row label="Condition" value={capitalise(asset.status)} />
        <Row
          label="Active threats"
          value={threats}
          colour={threats > 0 ? palette.warning : undefined}
        />
        {zone && <Row label="Protected by" value={zone.name} />}
      </Section>

      <section className={styles.threatBlock} style={{ borderColor: scoreColour(asset.protectionScore) }}>
        <div className={styles.threatTop}>
          <span className="heading">Protection</span>
        </div>
        <div className={styles.threatScore} style={{ color: scoreColour(asset.protectionScore) }}>
          {Math.round(asset.protectionScore * 100)}
          <span className={styles.threatScoreMax}>%</span>
        </div>
        <Bar value={asset.protectionScore} colour={scoreColour(asset.protectionScore)} />
        <p className={styles.note}>
          Detection coverage over this asset, weighted with response availability and
          communications.
        </p>
      </section>

      <Section title="Coverage">
        <Row label="Sensors online" value={`${covering.length} of ${site.sensors.length}`} />
        <Row
          label="Response available"
          value={`${available.length} of ${site.resources.length}`}
          colour={available.length < 2 ? palette.caution : undefined}
        />
      </Section>
    </>
  );
}

// ---------------------------------------------------------------------------
// Sensor
// ---------------------------------------------------------------------------

function SensorView({ sensor }: { sensor: Sensor }) {
  const colour =
    sensor.status === 'online'
      ? palette.nominal
      : sensor.status === 'degraded'
        ? palette.caution
        : palette.danger;

  const deg = (r: number) => `${Math.round((r * 180) / Math.PI)}°`;

  return (
    <>
      <header className={styles.header} style={{ borderColor: colour }}>
        <div className={styles.headerTop}>
          <span className={styles.subject}>{sensor.id}</span>
          <span className={styles.kind} style={{ color: colour }}>
            {sensor.status.toUpperCase()}
          </span>
        </div>
        <span className={styles.headerMeta}>{sensor.name}</span>
      </header>

      <Section title="Health">
        <Row label="Device health" value={`${Math.round(sensor.health * 100)}%`} />
        <Row
          label="Effectiveness"
          value={`${Math.round(sensor.effectiveness * 100)}%`}
          colour={scoreColour(sensor.effectiveness)}
        />
        <Bar value={sensor.effectiveness} colour={scoreColour(sensor.effectiveness)} />
        {sensor.degradationReason && (
          <p className={styles.note} style={{ color: palette.caution }}>
            {sensor.degradationReason}
          </p>
        )}
      </Section>

      <Section title="Coverage envelope">
        <Row label="Type" value={capitalise(sensor.type)} />
        <Row label="Range" value={`${sensor.coverage.minRange}–${sensor.coverage.maxRange} m`} />
        <Row
          label="Azimuth"
          value={
            sensor.coverage.azimuthWidth >= Math.PI * 2 - 0.01
              ? 'Omnidirectional'
              : `${deg(sensor.coverage.azimuthWidth)} on ${deg(sensor.coverage.bearing)}`
          }
        />
        <Row
          label="Elevation"
          value={`${deg(sensor.coverage.minElevation)} to ${deg(sensor.coverage.maxElevation)}`}
        />
        <Row label="Mast height" value={`${sensor.mastHeight} m`} />
      </Section>
    </>
  );
}

// ---------------------------------------------------------------------------
// System and site fallbacks
// ---------------------------------------------------------------------------

function SystemView({ health }: { health: SystemHealth }) {
  return (
    <>
      <header className={styles.header} style={{ borderColor: palette.holo }}>
        <div className={styles.headerTop}>
          <span className={styles.subject}>System</span>
          <span className={styles.kind}>SELF-ASSESSMENT</span>
        </div>
        <span className={styles.headerMeta}>Aetos monitoring Aetos</span>
      </header>

      <Section title="Subsystems">
        <div className={styles.subsystems}>
          {health.subsystems.map((sub) => (
            <div key={sub.id} className={styles.subsystem}>
              <div className={styles.subsystemTop}>
                <span className={styles.subsystemLabel}>{sub.label}</span>
                <span className={`${styles.subsystemValue} value`} style={{ color: scoreColour(sub.value) }}>
                  {sub.status === 'offline' && sub.value === 0 ? 'OFFLINE' : `${Math.round(sub.value * 100)}%`}
                </span>
              </div>
              <Bar value={sub.value} colour={scoreColour(sub.value)} />
            </div>
          ))}
        </div>
      </Section>

      {health.primaryWeakness && (
        <section className={styles.recommendation}>
          <h3 className="heading">Primary weakness</h3>
          <p className={styles.recommendationValue} style={{ color: palette.caution }}>
            {health.primaryWeakness}
          </p>
          {health.recommendedAction && (
            <p className={styles.recommendationRationale}>{health.recommendedAction}</p>
          )}
        </section>
      )}
    </>
  );
}

function SiteView() {
  const site = useAetos((s) => s.site);
  const snapshot = useAetos((s) => s.snapshot);

  const critical = site.assets
    .filter((a) => a.criticality >= 7)
    .map((a) => ({ ...a, ...(snapshot?.assets[a.id] ?? {}) }))
    .sort((a, b) => a.protectionScore - b.protectionScore);

  return (
    <>
      <header className={styles.header} style={{ borderColor: palette.holoDim }}>
        <div className={styles.headerTop}>
          <span className={styles.subject}>{site.name}</span>
          <span className={styles.kind}>{site.id}</span>
        </div>
        <span className={styles.headerMeta}>{site.kind}</span>
      </header>

      <Section title="Critical assets">
        <div className={styles.subsystems}>
          {critical.map((asset) => (
            <div key={asset.id} className={styles.subsystem}>
              <div className={styles.subsystemTop}>
                <span className={styles.subsystemLabel}>{asset.name}</span>
                <span
                  className={`${styles.subsystemValue} value`}
                  style={{ color: scoreColour(asset.protectionScore) }}
                >
                  {Math.round(asset.protectionScore * 100)}%
                </span>
              </div>
              <Bar value={asset.protectionScore} colour={scoreColour(asset.protectionScore)} />
            </div>
          ))}
        </div>
      </Section>

      {snapshot && (
        <Section title="Coverage">
          <Row label="Weakest sector" value={capitalise(snapshot.coverage.weakestSector)} />
          <Row
            label="Detection gaps"
            value={`${Math.round(
              (snapshot.coverage.cells.filter((c) => c.quality === 'gap').length /
                Math.max(snapshot.coverage.cells.length, 1)) *
                100,
            )}%`}
          />
        </Section>
      )}

      <p className={styles.hint}>
        Select a track, asset or sensor in the scene, or a row in the threat summary.
      </p>
    </>
  );
}

function capitalise(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
