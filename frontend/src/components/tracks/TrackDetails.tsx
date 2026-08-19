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

  const age = (track.updatedAt.getTime() - track.firstSeen.getTime()) / 1000
  const sinceSeen = (Date.now() - track.lastSeen.getTime()) / 1000

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
        <Row label="Last observed" value={`${sinceSeen.toFixed(0)} s ago`} />
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
        {/* Listed flat, and deliberately so. Which sensor contributed *most* is a weighting no
            stage computes — fusion reconciles tracks, not measurements, and refuses to merge
            estimates (ADR 0017) — so ranking them here would be inventing an order. */}
        <p className="details__note">
          Sensors that have held this track, unranked. No stage computes a contribution weight.
        </p>
      </Section>

      <FusionSection track={track} />

      <AssessmentPanel track={track} />
    </aside>
  )
}

/** What fusion made of this track: one object, or several tracks observing one.
 *
 *  Three states, rendered differently on purpose. Fusion never ran; fusion ran and this track
 *  stands alone; fusion ran and this track is one of several observing one object. The first
 *  two are the pair ADR 0011 exists to keep apart — "nobody looked" must not render as "looked
 *  and found nothing".
 *
 *  A supporting track is reached only through `?include_supporting=true`, so seeing one here at
 *  all means somebody went looking for it; it is told plainly which object it belongs to rather
 *  than being shown as though it were the contact itself. */
function FusionSection({ track }: { track: DisplayTrack }) {
  const fusion = track.fusion

  if (!fusion.assessed) {
    return (
      <Section title="Object">
        <p className="details__note">
          No fusion service is attached, so nothing has judged whether this track and others are
          the same object. Not the same as having judged that it stands alone.
        </p>
      </Section>
    )
  }

  if (!fusion.isPrimary) {
    return (
      <Section title="Object">
        <Row label="Object" value={fusion.objectId ?? '—'} />
        <p className="details__note">
          A supporting observation of that object. Another track is presented for it; this one
          is still tracked and still logged, and is shown here because it was asked for
          explicitly.
        </p>
      </Section>
    )
  }

  if (fusion.memberTrackIds.length <= 1) {
    return (
      <Section title="Object">
        <Row label="Object" value={fusion.objectId ?? '—'} />
        <p className="details__note">
          Assessed and found to stand alone — no other track is an observation of this object.
        </p>
      </Section>
    )
  }

  return (
    <Section title="Object">
      <Row label="Object" value={fusion.objectId ?? '—'} />
      <Row label="Observed by" value={`${fusion.memberTrackIds.length} tracks`} />
      <ul className="details__sensors">
        {fusion.memberTrackIds.map((id) => (
          <li key={id} className="numeric">
            {id}
            {id === track.id ? ' (presented)' : ''}
          </li>
        ))}
      </ul>
      <p className="details__note">
        These tracks are reconciled as one object. Their estimates are not combined — the
        position shown is this track&apos;s own, as the tracker produced it.
      </p>
      {fusion.corroboratingSensors.length > 0 && (
        <Row
          label="Corroborated by"
          value={`${fusion.corroboratingSensors.length} sensors across the object`}
        />
      )}
    </Section>
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
