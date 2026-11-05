/** Live tracks: the full table, with detail beside it. */

import { NAVIGATION } from '../../components/shell/navigation'
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

/**
 * A page's title, after the reference site's headlines: a small caps eyebrow, then the title
 * with its first word in the italic serif. Each word unblurs into place.
 */
export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  const words = title.split(' ')
  const number = NAVIGATION.flatMap((s) => s.items).find((i) => i.label === title)?.number
  return (
    <header className="page__header">
      <span className="page__subtitle label">
        {number && <span className="page__number numeric">{number} /</span>}
        {subtitle}
      </span>
      <h1 className="page__title" aria-label={title}>
        {words.map((word, i) => (
          <span
            key={`${word}-${i}`}
            aria-hidden="true"
            className={`reveal-word ${i === 0 ? 'serif' : ''}`}
            style={{ animationDelay: `${80 + i * 90}ms` }}
          >
            {word}
            {i < words.length - 1 ? '\u00a0' : ''}
          </span>
        ))}
      </h1>
    </header>
  )
}
