'use client';

/**
 * Left rail: the threat picture.
 *
 * Counts at the top, then every track ranked by how much it warrants attention. The list
 * is the primary way an operator moves around the scene — selecting a row flies the camera
 * to that track — so it is ordered by severity rather than by track number, and the row
 * carries enough context to decide without opening it.
 */

import { palette, threatColour, threatLabel } from '@/lib/palette';
import { labelFor } from '@/simulation/threat';
import type { ThreatLevel, Track } from '@/types';
import { useAetos, useRankedTracks, useThreatCounts } from '@/state/store';
import styles from './threat.module.css';

const LEVELS: ThreatLevel[] = ['critical', 'high', 'medium', 'low'];

export function ThreatSummary() {
  const counts = useThreatCounts();
  const tracks = useRankedTracks();
  const select = useAetos((s) => s.select);
  const selectedId = useAetos((s) => (s.selection?.kind === 'track' ? s.selection.id : null));

  return (
    <aside className={`${styles.rail} panel`}>
      <div className={styles.header}>
        <h2 className="heading">Threat summary</h2>
      </div>

      <div className={styles.counts}>
        {LEVELS.map((level) => (
          <div key={level} className={styles.count}>
            <span className={styles.countBar} style={{ background: threatColour[level] }} />
            <span className={styles.countLabel}>{threatLabel[level]}</span>
            <span
              className={`${styles.countValue} value`}
              style={{ color: counts[level] > 0 ? threatColour[level] : palette.textFaint }}
            >
              {counts[level]}
            </span>
          </div>
        ))}
      </div>

      <div className={styles.header}>
        <h2 className="heading">Active tracks</h2>
        <span className={styles.total}>{counts.total}</span>
      </div>

      <div className={styles.list}>
        {tracks.length === 0 && <p className={styles.empty}>No tracks held. Airspace clear.</p>}

        {tracks.map((track) => (
          <TrackRow
            key={track.trackId}
            track={track}
            selected={track.trackId === selectedId}
            onSelect={() => select({ kind: 'track', id: track.trackId })}
          />
        ))}
      </div>
    </aside>
  );
}

function TrackRow({
  track,
  selected,
  onSelect,
}: {
  track: Track;
  selected: boolean;
  onSelect: () => void;
}) {
  const authorised = track.identity === 'authorised';
  const colour = authorised ? palette.authorised : threatColour[track.threat.level];

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`${styles.row} ${selected ? styles.rowSelected : ''}`}
      style={{ borderLeftColor: colour }}
    >
      <div className={styles.rowTop}>
        <span className={styles.rowId}>{track.trackId}</span>
        <span className={`${styles.rowScore} value`} style={{ color: colour }}>
          {authorised ? 'AUTH' : track.threat.score}
        </span>
      </div>

      <div className={styles.rowMeta}>
        <span>{labelFor(track.classification)}</span>
        <span className={styles.dot}>·</span>
        <span className="value">{Math.round(track.range)} m</span>
        <span className={styles.dot}>·</span>
        <span className="value">{Math.round(track.altitude)} m</span>
      </div>

      {/* The one line that explains the ranking. Without it the list is a scoreboard. */}
      <div className={styles.rowReason} style={{ color: selected ? palette.text : palette.textFaint }}>
        {reasonFor(track)}
      </div>
    </button>
  );
}

/**
 * The single most useful sentence about a track, chosen in priority order: what it is
 * about to do beats what it is doing, which beats what it is.
 */
function reasonFor(track: Track): string {
  const { etaToProtectedZone, factors } = track.threat;

  if (etaToProtectedZone !== undefined && etaToProtectedZone < 120) {
    return `Protected volume in ${Math.round(etaToProtectedZone)} s`;
  }

  const incursion = factors.find((f) => f.code === 'INCURSION');
  if (incursion) return incursion.label;

  const behaviour = factors.find((f) => f.code === 'BEHAVIOUR');
  if (behaviour && track.threat.level !== 'low') return behaviour.label;

  if (track.identity === 'authorised') return 'Authorised flight';
  return `${track.behaviour.charAt(0).toUpperCase()}${track.behaviour.slice(1)}`;
}
