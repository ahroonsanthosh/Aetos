/**
 * Overview: the operational picture. The map dominates, with the selected track beside it and
 * the track list beneath.
 *
 * The situation banner sits above all of it. Its findings — contacts converging from separate
 * bearings, more objects engaged than an operator can attend to — belong to the picture as a
 * whole and would be invisible anywhere a single track is displayed.
 */

import { useEffect, useState } from 'react'

import { SituationBanner } from '../../components/assessment/SituationBanner'
import { OperationalMap } from '../../components/map/OperationalMap'
import { TrackDetails } from '../../components/tracks/TrackDetails'
import { TrackList } from '../../components/tracks/TrackList'
import { api } from '../../data/client/api'
import { useSituation } from '../../data/client/useSituation'
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
  const { situation } = useSituation()
  const selected = picture.tracks.find((t) => t.id === selectedTrackId) ?? null

  return (
    <div className="page page--operational">
      {picture.error && <div className="page__error">{picture.error}</div>}

      {/* Above the map, because it is about the whole picture rather than anything in it.
          Renders nothing when there is nothing to say — a permanent "situation: normal" bar
          is one an operator stops seeing, and then its absence means nothing either. */}
      <SituationBanner situation={situation} onSelectTrack={onSelectTrack} />

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
