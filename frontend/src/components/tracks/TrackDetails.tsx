/**
 * Selected track detail.
 *
 * Every value shown here comes from the backend. Range and bearing are the one exception —
 * they are derived from the track's geodetic position relative to the site reference, and are
 * labelled site-relative because no sensor measured them (ADR 0010).
 *
 * A field the backend did not supply renders as an em dash. There is no interpolation, no
 * carrying forward of a previous value, and no default that could be mistaken for a reading.
 */

import type { DisplayTrack } from '../../data/models/track'
import { compassPoint } from '../../data/models/geo'
import { AssessmentPanel } from '../assessment/AssessmentPanel'
import './TrackDetails.css'

interface Props {
  track: DisplayTrack | null
}

export function TrackDetails({ track }: Props) {
  if (!track) {
    return (
      <aside className="details details--empty">
        <span className="label">No track selected</span>
      </aside>
    )
  }

  // Both figures come from the backend's own clock. `age` is a difference between two
  // backend timestamps; staleness is reported by the backend outright.
  //
  // It used to be `Date.now() - track.lastSeen`, which subtracts this browser's clock from the
  // *sensor's* — two machines this interface does not control, and whose drift landed in the
  // number an operator reads as "last observed". A laptop a minute out of sync showed every
  // track, however healthy, as a minute stale.
  const age = (track.updatedAt.getTime() - track.firstSeen.getTime()) / 1000
  const sinceSeen = track.secondsSinceDetection

  return (
    <aside className="details">
      <header className="details__header">
        <div className="details__id numeric">{track.id}</div>
        <div className={`details__state details__state--${track.state.toLowerCase()}`}>
          {track.state}
        </div>
        <p className="details__state-description">{track.stateDescription}</p>
      </header>

      <Section title="Position">
        <Row label="Range" value={`${track.siteRelative.rangeM.toFixed(0)} m`} note="from site" />
        <Row
          label="Bearing"
          value={`${track.siteRelative.bearingDeg.toFixed(1)}° ${compassPoint(track.siteRelative.bearingDeg)}`}
          note="from site"
        />
        <Row label="Altitude" value={`${track.position.altitudeM.toFixed(0)} m`} note="ellipsoidal" />
        <Row label="Latitude" value={track.position.latitude.toFixed(6)} />
        <Row label="Longitude" value={track.position.longitude.toFixed(6)} />
      </Section>

      <Section title="Kinematics">
        <Row label="Speed" value={`${track.speedMps.toFixed(1)} m/s`} />
        <Row
          label="Heading"
          value={`${track.headingDeg.toFixed(1)}° ${compassPoint(track.headingDeg)}`}
        />
        <Row label="Vertical rate" value={`${track.verticalRateMps.toFixed(1)} m/s`} />
        <Row label="Track age" value={`${age.toFixed(0)} s`} />
        {/* Estimated by the filter from a sequence of positions. The backend is never told a
            speed or a heading by a sensor — see ADR 0002. */}
        <p className="details__note">
          Speed and heading are estimated from observed positions, not reported by a sensor.
        </p>
      </Section>

      <Section title="Track Quality">
        <Row label="Confidence" value={`${(track.trackConfidence * 100).toFixed(0)}%`} />
        <Row label="Observations" value={String(track.detectionCount)} />
        <Row label="Missed updates" value={String(track.missedUpdates)} />
        <Row
          label="Last observed"
          value={sinceSeen === null ? '—' : `${sinceSeen.toFixed(0)} s ago`}
          note={sinceSeen === null ? 'not reported' : undefined}
        />
        <Row label="Coasting" value={track.coasting ? 'Yes — position predicted' : 'No'} />
      </Section>

      <Section title="Sensor Sources">
        {track.contributingSensors.length ? (
          <ul className="details__sensors">
            {track.contributingSensors.map((sensor) => (
              <li key={sensor} className="numeric">
                {sensor}
              </li>
            ))}
          </ul>
        ) : (
          <Row label="Sensors" value="—" />
        )}
        {/* Which sensor is primary and which supporting is a fusion judgement, and fusion is
            not implemented. Listing them flat is the honest presentation. */}
        <p className="details__note">
          Contribution weighting requires the fusion module, which is not implemented.
        </p>
      </Section>

      <AssessmentPanel track={track} />
    </aside>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="details__section">
      <div className="label details__section-title">{title}</div>
      {children}
    </section>
  )
}

function Row({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="details__row">
      <span className="label">{label}</span>
      <span className="details__value numeric">
        {value}
        {note && <span className="details__note-inline">{note}</span>}
      </span>
    </div>
  )
}
