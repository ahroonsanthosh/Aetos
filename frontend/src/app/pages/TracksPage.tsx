/** Live tracks: the full table, with detail beside it. */

import { TrackDetails } from '../../components/tracks/TrackDetails'
import { TrackList } from '../../components/tracks/TrackList'
import type { TrackPicture } from '../../data/client/useTrackPicture'
import './pages.css'

interface Props {
  picture: TrackPicture
  selectedTrackId: string | null
  onSelectTrack: (trackId: string | null) => void
}

export function TracksPage({ picture, selectedTrackId, onSelectTrack }: Props) {
  const selected = picture.tracks.find((t) => t.id === selectedTrackId) ?? null

  return (
    <div className="page">
      <PageHeader title="Live Tracks" subtitle={`${picture.tracks.length} maintained`} />
      <div className="page__main">
        <div className="page__fill">
          <TrackList
            tracks={picture.tracks}
            selectedTrackId={selectedTrackId}
            onSelectTrack={onSelectTrack}
          />
        </div>
        <TrackDetails track={selected} />
      </div>
    </div>
  )
}

export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <header className="page__header">
      <h1 className="page__title">{title}</h1>
      {subtitle && <span className="page__subtitle label">{subtitle}</span>}
    </header>
  )
}
