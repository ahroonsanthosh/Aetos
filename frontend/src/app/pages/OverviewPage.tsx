/**
 * Overview: the operational picture. The map dominates, with the selected track beside it and
 * the track list beneath.
 */

import { useEffect, useState } from 'react'

import { OperationalMap } from '../../components/map/OperationalMap'
import { TrackDetails } from '../../components/tracks/TrackDetails'
import { TrackList } from '../../components/tracks/TrackList'
import { api } from '../../data/client/api'
import type { SensorView } from '../../data/models/api'
import type { TrackPicture } from '../../data/client/useTrackPicture'
import './pages.css'

interface Props {
  picture: TrackPicture
  selectedTrackId: string | null
  onSelectTrack: (trackId: string | null) => void
}

export function OverviewPage({ picture, selectedTrackId, onSelectTrack }: Props) {
  const sensors = useSensors()
  const selected = picture.tracks.find((t) => t.id === selectedTrackId) ?? null

  return (
    <div className="page page--operational">
      {picture.error && <div className="page__error">{picture.error}</div>}

      <div className="page__main">
        <OperationalMap
          site={picture.site}
          frame={picture.frame}
          sensors={sensors}
          tracks={picture.tracks}
          selectedTrackId={selectedTrackId}
          onSelectTrack={onSelectTrack}
        />
        <TrackDetails track={selected} />
      </div>

      <div className="page__drawer">
        <TrackList
          tracks={picture.tracks}
          selectedTrackId={selectedTrackId}
          onSelectTrack={onSelectTrack}
        />
      </div>
    </div>
  )
}

/** Sensor placements. Configuration, so it is fetched once rather than streamed. */
export function useSensors(): SensorView[] {
  const [sensors, setSensors] = useState<SensorView[]>([])

  useEffect(() => {
    const abort = new AbortController()
    api
      .sensors(abort.signal)
      .then((response) => setSensors(response.sensors))
      .catch(() => {
        // The map is still useful without sensor markers, and the system health page is where
        // a backend problem gets reported. Failing silently here beats an error banner over
        // the operational picture for a secondary overlay.
      })
    return () => abort.abort()
  }, [])

  return sensors
}
