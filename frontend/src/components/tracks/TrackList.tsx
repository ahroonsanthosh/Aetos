/**
 * Tabular track list.
 *
 * Ordered by identifier rather than by range or risk. Sorting by a quantity that changes every
 * update cycle makes rows jump under the operator's cursor, and a list you cannot point at is
 * worse than one that is not sorted the way you would like.
 */

import type { DisplayTrack } from '../../data/models/track'
import './TrackList.css'

interface Props {
  tracks: readonly DisplayTrack[]
  selectedTrackId: string | null
  onSelectTrack: (trackId: string) => void
}

export function TrackList({ tracks, selectedTrackId, onSelectTrack }: Props) {
  if (!tracks.length) {
    return (
      <div className="track-list track-list--empty">
        <span className="label">No tracks</span>
        <span className="track-list__hint">
          Nothing is currently being tracked. Start a scenario to generate synthetic detections.
        </span>
      </div>
    )
  }

  return (
    <div className="track-list">
      <table>
        <thead>
          <tr>
            <th className="label">Track</th>
            <th className="label">State</th>
            <th className="label">Range</th>
            <th className="label">Bearing</th>
            <th className="label">Alt</th>
            <th className="label">Speed</th>
            <th className="label">Hdg</th>
            <th className="label" title="Confidence the track exists at all">Conf</th>
            <th className="label">Obs</th>
            {/* Two columns, not one. Both are implemented now, but they answer different
                questions and can disagree — a confidently classified bird still gets a risk
                score, and an unclassified track can still be scored on geometry alone.
                Collapsing them would hide which of the two is the unassessed one. */}
            <th className="label">Class</th>
            <th className="label">Risk</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((track) => (
            <tr
              key={track.id}
              className={track.id === selectedTrackId ? 'is-selected' : ''}
              onClick={() => onSelectTrack(track.id)}
            >
              <td className="numeric">{track.id}</td>
              <td>
                <span className={`track-list__state track-list__state--${track.state.toLowerCase()}`}>
                  {track.state}
                </span>
              </td>
              <td className="numeric">{track.siteRelative.rangeM.toFixed(0)} m</td>
              <td className="numeric">{track.siteRelative.bearingDeg.toFixed(0)}°</td>
              <td className="numeric">{track.position.altitudeM.toFixed(0)} m</td>
              <td className="numeric">{track.speedMps.toFixed(1)}</td>
              <td className="numeric">{track.headingDeg.toFixed(0)}°</td>
              <td className="numeric">{(track.trackConfidence * 100).toFixed(0)}%</td>
              <td className="numeric">{track.detectionCount}</td>
              <td className="track-list__assessment">
                {track.assessment.classified ? (
                  <>
                    {track.assessment.label.replace(/_/g, ' ').toUpperCase()}
                    <span className="track-list__confidence">
                      {(track.assessment.labelConfidence * 100).toFixed(0)}%
                    </span>
                  </>
                ) : (
                  <span
                    className="track-list__unassessed"
                    title="No evidence has reached classification for this track yet"
                  >
                    not assessed
                  </span>
                )}
              </td>
              <td className="track-list__assessment">
                {track.assessment.riskAssessed ? (
                  track.assessment.riskLevel.toUpperCase()
                ) : (
                  <span
                    className="track-list__unassessed"
                    title="Risk has not yet scored this track — usually a track created within the last cycle"
                  >
                    not assessed
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
