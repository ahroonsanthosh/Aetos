/**
 * The 3-D digital twin, inside the console.
 *
 * The twin is its own application (`twin/`), built to static files and served by the backend at
 * `/twin/`. It is framed here rather than linked to so the operator keeps the console's
 * navigation and status bar around it. The backend allows exactly this framing: the twin's pages
 * say `frame-ancestors 'self'`, so only this origin can embed them.
 *
 * The twin runs on its own simulated picture, not on this console's live tracks, and the page
 * says so rather than letting a second display of drones read as a second view of the same sky.
 */

import { PageHeader } from './TracksPage'
import './pages.css'
import './DigitalTwinPage.css'

export const TWIN_URL = '/twin/'

export function DigitalTwinPage() {
  return (
    <div className="page">
      <PageHeader title="Digital Twin" subtitle="3-D site model · simulated picture" />
      <div className="twin-page__bar">
        <span>
          The twin runs its own simulated scenario over a 3-D model of the site. It is not fed
          by this console's live tracks yet.
        </span>
        <a className="twin-page__open" href={TWIN_URL} target="_blank" rel="noreferrer">
          Open full screen ↗
        </a>
      </div>
      <iframe className="twin-page__frame" src={TWIN_URL} title="Aetos digital twin" />
    </div>
  )
}
