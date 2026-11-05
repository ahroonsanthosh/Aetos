/**
 * The 3-D digital twin, inside the console.
 *
 * The twin is its own application (`twin/`), built to static files and served by the backend at
 * `/twin/`. It is framed here rather than linked to so the operator keeps the console's
 * navigation and status bar around it. The backend allows exactly this framing: the twin's pages
 * say `frame-ancestors 'self'`, so only this origin can embed them.
 *
 * The twin reads this console's own site and tracks (its Live feed), and can switch to its
 * built-in scenarios; its top bar says which, so a simulated picture is never mistaken for the
 * live one.
 */

import { PageHeader } from './TracksPage'
import './pages.css'
import './DigitalTwinPage.css'

export const TWIN_URL = '/twin/'

export function DigitalTwinPage() {
  return (
    <div className="page">
      <PageHeader title="Digital Twin" subtitle="3-D site model · live tracks" />
      <div className="twin-page__bar">
        <span>
          The live site and its tracks in 3-D, with Aetos's own risk scores. Switch to the
          twin's built-in scenarios from its Feed controls.
        </span>
        <a className="pill pill--ink" href={TWIN_URL} target="_blank" rel="noreferrer">
          Open full screen
        </a>
      </div>
      <iframe className="twin-page__frame" src={TWIN_URL} title="Aetos digital twin" />
    </div>
  )
}
