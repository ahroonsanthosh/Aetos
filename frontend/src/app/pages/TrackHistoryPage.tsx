/**
 * Track History: review the full recorded life of any known track.
 *
 * The operational map's trail (`components/map/TrackTrail`) shows this live for whichever
 * track is currently selected, and clears the moment it is deselected, by design — the map
 * should never accumulate history it has not been explicitly asked to show. This page is the
 * complementary, durable view: pick a track and review its whole record without needing to
 * keep it selected, including tracks that have already gone LOST — history is retained
 * permanently (see FM-022; `GET /tracks/{id}/history` never forgets).
 *
 * Track lookup has two paths, and the distinction matters. The dropdown lists tracks the live
 * picture currently knows about. A track that has already left the live picture (state LOST,
 * dropped from `GET /tracks`) can still be reviewed *if its ID is already known* — from the
 * Event Log, say — through the manual field beside it. There is no endpoint that enumerates
 * every track ID the system has ever created, so a track can be looked up here once it has
 * left the live list, but not discovered.
 */

import { useEffect, useMemo, useState } from 'react'

import { HistorySparkline } from '../../components/tracks/HistorySparkline'
import { api, ApiError } from '../../data/client/api'
import type { TrackPicture } from '../../data/client/useTrackPicture'
import { toHistoryPoint, type TrackHistoryPoint } from '../../data/models/track'
import { PageHeader } from './TracksPage'
import './pages.css'
import './TrackHistoryPage.css'

interface Props {
  picture: TrackPicture
}

export function TrackHistoryPage({ picture }: Props) {
  const [trackId, setTrackId] = useState('')
  const [manualId, setManualId] = useState('')
  const [points, setPoints] = useState<TrackHistoryPoint[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const knownIds = useMemo(
    () => [...picture.tracks].map((t) => t.id).sort(),
    [picture.tracks],
  )

  useEffect(() => {
    const frame = picture.frame
    if (!trackId || !frame) {
      setPoints(null)
      setError(null)
      return
    }

    const abort = new AbortController()
    setLoading(true)

    api
      .trackHistory(trackId, abort.signal)
      .then((response) => {
        setPoints(response.points.map((point) => toHistoryPoint(point, frame)))
        setError(null)
      })
      .catch((cause: unknown) => {
        if (abort.signal.aborted) return
        setPoints(null)
        setError(cause instanceof ApiError ? cause.message : `Could not load history: ${cause}`)
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false)
      })

    return () => abort.abort()
  }, [trackId, picture.frame])

  function lookUp() {
    const id = manualId.trim()
    if (id) setTrackId(id)
  }

  return (
    <div className="page">
      <PageHeader
        title="Track History"
        subtitle={points ? `${points.length} recorded points` : undefined}
      />
      <div className="page__body">
        <p className="page__note">
          The map shows a selected track&rsquo;s route live and clears it the moment the track
          is deselected. This page is the durable record: pick any track the system currently
          knows about, or enter an ID directly for one that has already gone LOST — history is
          retained permanently, never deleted.
        </p>

        <div className="history-picker">
          <label className="history-picker__field">
            <span className="label">Known tracks</span>
            <select
              value={knownIds.includes(trackId) ? trackId : ''}
              onChange={(changeEvent) => {
                setTrackId(changeEvent.target.value)
                setManualId('')
              }}
            >
              <option value="">Select a track</option>
              {knownIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>

          <label className="history-picker__field">
            <span className="label">Or look up a track ID</span>
            <div className="history-picker__manual">
              <input
                type="text"
                placeholder="TRK-00042"
                value={manualId}
                onChange={(changeEvent) => setManualId(changeEvent.target.value)}
                onKeyDown={(keyEvent) => {
                  if (keyEvent.key === 'Enter') lookUp()
                }}
              />
              <button type="button" onClick={lookUp} disabled={!manualId.trim()}>
                Look up
              </button>
            </div>
          </label>
        </div>

        {!trackId && (
          <div className="panel">
            <div className="empty-state">
              <div className="empty-state__title">No track selected</div>
              <p className="empty-state__body">
                Choose a track above to review its recorded history.
              </p>
            </div>
          </div>
        )}

        {trackId && loading && (
          <div className="panel">
            <div className="empty-state">
              <span className="label">Loading…</span>
            </div>
          </div>
        )}

        {trackId && !loading && error && (
          <div className="panel">
            <div className="empty-state">
              <div className="empty-state__title">Could not load history</div>
              <p className="empty-state__body">{error}</p>
            </div>
          </div>
        )}

        {trackId && !loading && !error && points && points.length === 0 && (
          <div className="panel">
            <div className="empty-state">
              <div className="empty-state__title">No history recorded</div>
              <p className="empty-state__body">
                {trackId} has no recorded history. Check the ID and try again.
              </p>
            </div>
          </div>
        )}

        {trackId && !loading && !error && points && points.length > 0 && (
          <>
            <div className="panel history-charts">
              <div className="history-chart">
                <span className="label">Altitude</span>
                <HistorySparkline
                  points={points.map((p) => ({ timestamp: p.timestamp, value: p.altitudeM }))}
                  unit="m"
                />
              </div>
              <div className="history-chart">
                <span className="label">Speed</span>
                <HistorySparkline
                  points={points.map((p) => ({ timestamp: p.timestamp, value: p.speedMps }))}
                  unit="m/s"
                />
              </div>
            </div>

            <div className="panel">
              <table className="history-table">
                <thead>
                  <tr>
                    <th className="label">Seq</th>
                    <th className="label">Time</th>
                    <th className="label">Event</th>
                    <th className="label">State</th>
                    <th className="label">Altitude</th>
                    <th className="label">Speed</th>
                    <th className="label">Heading</th>
                  </tr>
                </thead>
                <tbody>
                  {points.map((point) => (
                    <tr key={point.seq}>
                      <td className="numeric">{point.seq}</td>
                      <td className="numeric">{point.timestamp.toLocaleTimeString()}</td>
                      <td>{point.eventType.replace('TRACK_', '')}</td>
                      <td>{point.state}</td>
                      <td className="numeric">{point.altitudeM.toFixed(0)} m</td>
                      <td className="numeric">{point.speedMps.toFixed(1)} m/s</td>
                      <td className="numeric">{point.headingDeg.toFixed(0)}°</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
