/**
 * Routing, the public entry, and the shared live picture.
 *
 * Three screens: the front page, the sign-in card, and the console. A visitor gets the first
 * two in that order; an operator who already has a session goes straight to the console.
 * `/landing` renders the front page unconditionally, so it can be shown on purpose.
 *
 * ## The live picture
 *
 * Established once here and passed down, rather than each page opening its own connection. One
 * event stream per client means one resume position, and therefore one consistent answer to
 * "what is out there" across every view — two streams would drift and two pages would disagree.
 */

import { useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'

import { AetosHero } from '../components/landing/AetosHero'
import { AppShell } from '../components/shell/AppShell'
import { useAuth } from '../data/client/auth'
import { useTrackPicture } from '../data/client/useTrackPicture'
import { currentIdentity } from '../data/models/succession'
import { AlertsPage } from './pages/AlertsPage'
import { CameraPage } from './pages/CameraPage'
import { DigitalTwinPage } from './pages/DigitalTwinPage'
import { LoginPage } from './pages/LoginPage'
import { EventLogPage } from './pages/EventLogPage'
import { OverviewPage } from './pages/OverviewPage'
import { ResponseSystemsPage } from './pages/ResponseSystemsPage'
import { SensorsPage } from './pages/SensorsPage'
import { SystemHealthPage } from './pages/SystemHealthPage'
import { TrackHistoryPage } from './pages/TrackHistoryPage'
import { TracksPage } from './pages/TracksPage'

/** Set once a session has been established in this tab, so the boot sequence plays on the way
 *  in and not again on every re-render or route change. Session storage rather than component
 *  state: a full page reload of an authenticated console should go straight to the picture. */

export function App() {
  const { status } = useAuth()

  // Nothing renders and no stream opens until the session is known. Splitting the console into
  // its own component is what makes that true: a hook cannot be called conditionally, so
  // leaving `useTrackPicture` in this component would open the WebSocket while the landing or
  // login page was on screen — and that connection carries the entire live picture.
  if (status === 'checking') return <Booting />
  if (status !== 'signed-in') return <PublicEntry />
  return <AuthenticatedConsole />
}

/** Deliberately almost empty. The alternative is a flash of the landing page on every load for
 *  someone already signed in, which teaches operators that the console logs them out at
 *  random. */
function Booting() {
  return <div className="app-booting">Checking session…</div>
}

/** What an unauthenticated visitor sees: the front page, then the sign-in card. */
function PublicEntry() {
  const [stage, setStage] = useState<'landing' | 'login'>('landing')

  return (
    <Routes>
      {/* Reachable on purpose, regardless of session, so the front page can be shown without
          signing out. */}
      <Route path="/landing" element={<AetosHero onLaunch={() => undefined} />} />
      <Route
        path="*"
        element={
          stage === 'login' ? <LoginPage /> : <AetosHero onLaunch={() => setStage('login')} />
        }
      />
    </Routes>
  )
}

/** The console. `/landing` is matched here, outside `AppShell`, because the front page is a
 *  full-viewport screen of its own. */
function AuthenticatedConsole() {
  return (
    <Routes>
      <Route path="/landing" element={<LandingFromConsole />} />
      <Route path="*" element={<Console />} />
    </Routes>
  )
}

function Console() {
  const picture = useTrackPicture()
  const [selectedTrackId, setSelectedTrackId] = useState<string | null>(null)

  // When the backend retires the selected track as a duplicate, the selection follows the
  // aircraft to the track that carries on — rather than the details panel going blank while
  // the object is still in the air under another identifier (ADR 0021).
  useEffect(() => {
    if (selectedTrackId === null) return
    const current = currentIdentity(selectedTrackId, picture.mergedInto)
    if (current !== selectedTrackId) setSelectedTrackId(current)
  }, [selectedTrackId, picture.mergedInto])

  return (
    <AppShell streamStatus={picture.status} trackCount={picture.tracks.length}>
      <Routes>
        <Route
          path="/"
          element={
            <OverviewPage
              picture={picture}
              selectedTrackId={selectedTrackId}
              onSelectTrack={setSelectedTrackId}
            />
          }
        />
        <Route
          path="/tracks"
          element={
            <TracksPage
              picture={picture}
              selectedTrackId={selectedTrackId}
              onSelectTrack={setSelectedTrackId}
            />
          }
        />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/digital-twin" element={<DigitalTwinPage />} />
        <Route path="/camera" element={<CameraPage />} />
        <Route path="/history" element={<TrackHistoryPage picture={picture} />} />
        <Route path="/sensors" element={<SensorsPage />} />
        <Route path="/health" element={<SystemHealthPage />} />
        <Route path="/events" element={<EventLogPage />} />
        <Route path="/response" element={<ResponseSystemsPage />} />

        {/* Anything unrecognised returns to the operational picture rather than showing an
            error page. An operator who mistypes a URL should land on the thing they are
            there to look at. */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  )
}

/** `/landing` for someone already signed in: show the front page, and let the CTA take them
 *  back to the console rather than to a sign-in screen they do not need. */
function LandingFromConsole() {
  const navigate = useNavigate()
  return <AetosHero onLaunch={() => navigate('/')} />
}
