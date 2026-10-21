/**
 * Routing, the public entry, and the shared live picture.
 *
 * ## Two audiences, two front doors
 *
 * A visitor arriving at the root gets the landing page: the cinematic entry, the launch
 * transition, then the sign-in screen. An operator who already has a session gets the console
 * immediately and never sees any of it.
 *
 * That split is deliberate and it is the whole reason the landing page is not simply the first
 * route. This console is used for hours at a time; making the people who actually work in it
 * sit through an entrance animation on every load would be the single most annoying thing in
 * the product. The landing page exists to introduce the system to someone meeting it for the
 * first time, and it should get out of the way of everybody else.
 *
 * `/landing` renders it unconditionally, so it can be shown on purpose — a demonstration, a
 * screenshot, or a signed-in operator who wants to see the front page.
 *
 * ## Why the boot sequence plays after authentication, not before
 *
 * The specification allowed either. It plays after, because "SYSTEM ONLINE" followed by a login
 * form is a claim that has not been earned yet — the system is precisely *not* online for that
 * person until they are through the door. The wipe still fires on the CTA, so the click has its
 * moment; the boot then covers the real work of mounting the console.
 *
 * ## The live picture
 *
 * Established once here and passed down, rather than each page opening its own connection. One
 * event stream per client means one resume position, and therefore one consistent answer to
 * "what is out there" across every view — two streams would drift and two pages would disagree.
 */

import { useCallback, useEffect, useState } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'

import { AetosHero } from '../components/landing/AetosHero'
import { LaunchSequence } from '../components/landing/LaunchSequence'
import { AppShell } from '../components/shell/AppShell'
import { useAuth } from '../data/client/auth'
import { useTrackPicture } from '../data/client/useTrackPicture'
import { currentIdentity } from '../data/models/succession'
import { AlertsPage } from './pages/AlertsPage'
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
const BOOTED_KEY = 'aetos.booted'

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

/**
 * What an unauthenticated visitor sees: the landing page, then the sign-in screen.
 *
 * The wipe runs between them. `stage` is local because this is a presentation sequence rather
 * than application state — nothing outside this component needs to know which half of the front
 * door is on screen.
 */
function PublicEntry() {
  const [stage, setStage] = useState<'landing' | 'wiping' | 'login'>('landing')

  return (
    <Routes>
      {/* Reachable on purpose, regardless of session, so the front page can be demonstrated
          and screenshotted without signing out. */}
      <Route path="/landing" element={<AetosHero onLaunch={() => undefined} />} />
      <Route
        path="*"
        element={
          <>
            {stage === 'login' ? <LoginPage /> : <AetosHero onLaunch={() => setStage('wiping')} />}
            {stage === 'wiping' && <LaunchSequence onComplete={() => setStage('login')} />}
          </>
        }
      />
    </Routes>
  )
}

/** The console, with the boot sequence in front of it the first time in.
 *
 *  `/landing` is matched here, outside `AppShell`, because the hero is a full-viewport
 *  composition — rendering it inside the shell would frame it with a sidebar and top bar and
 *  destroy the thing it exists to do. */
function AuthenticatedConsole() {
  const [booting, setBooting] = useState(() => readBooted() !== 'yes')

  const finish = useCallback(() => {
    writeBooted()
    setBooting(false)
  }, [])

  return (
    <Routes>
      <Route path="/landing" element={<LandingFromConsole />} />
      <Route
        path="*"
        element={
          <>
            <Console />
            {booting && <LaunchSequence onComplete={finish} />}
          </>
        }
      />
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

/** Session storage throws in some locked-down and private browsing modes. This runs while the
 *  console mounts, so an unguarded access would take the operator's whole display down over an
 *  animation preference; failing just means the boot sequence plays again next time. */
function readBooted(): string | null {
  try {
    return window.sessionStorage.getItem(BOOTED_KEY)
  } catch {
    return null
  }
}

function writeBooted(): void {
  try {
    window.sessionStorage.setItem(BOOTED_KEY, 'yes')
  } catch {
    // Not remembered; the sequence replays on the next load. Harmless.
  }
}
