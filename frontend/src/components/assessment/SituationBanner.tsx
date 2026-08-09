/**
 * What the tracks mean together, above the map where the whole picture is.
 *
 * Findings here are the ones no single track can produce — several contacts converging from
 * separate bearings, more objects engaged at once than an operator can reasonably attend to.
 * A track selected from the list will never show them, because they are not properties of it.
 *
 * Two rules this component follows.
 *
 * **Nothing is shown when nothing was found.** A permanent "situation: normal" bar is a bar an
 * operator stops seeing, and its absence then means nothing either. This appears when there is
 * something to say and is otherwise not there at all.
 *
 * **Clicking a finding selects its tracks.** A finding an operator cannot get from the banner
 * to the contacts is one they have to go and find by hand, which is exactly the work the
 * banner was supposed to save.
 */

import type { SituationView } from '../../data/models/api'
import './SituationBanner.css'

interface Props {
  situation: SituationView | null
  onSelectTrack: (trackId: string | null) => void
}

/** Findings that describe the *display* rather than the airspace. Rendered in a quieter
 *  treatment: "the map is over-counting" is important and is not a threat. */
const ADVISORY_CODES = new Set(['duplicate_tracks_suspected'])

export function SituationBanner({ situation, onSelectTrack }: Props) {
  if (!situation?.assessed || situation.findings.length === 0) return null

  return (
    <section className="situation" aria-label="Situation assessment">
      {situation.findings.map((finding) => {
        const advisory = ADVISORY_CODES.has(finding.code)
        return (
          <button
            key={finding.code}
            type="button"
            className={`situation__finding situation__finding--${
              advisory ? 'advisory' : finding.level
            }`}
            onClick={() => onSelectTrack(finding.track_ids[0] ?? null)}
            title={
              finding.track_ids.length
                ? `Select ${finding.track_ids[0]} (${finding.track_ids.length} tracks involved)`
                : undefined
            }
          >
            <span className="label situation__code">
              {advisory ? 'Advisory' : finding.level.toUpperCase()}
            </span>
            <span className="situation__detail">{finding.detail}</span>
            <span className="numeric situation__count">{finding.track_ids.length} tracks</span>
          </button>
        )
      })}

      {/* The object count sits beside the track count rather than replacing it. The gap
          between them is the missing sensor fusion made visible, and an operator reading
          "7 tracks · likely 3 objects" knows not to count aircraft off the display. */}
      <div className="situation__tally numeric">
        {situation.track_count} tracks · likely {situation.object_count} object
        {situation.object_count === 1 ? '' : 's'}
      </div>
    </section>
  )
}
