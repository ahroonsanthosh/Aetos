/**
 * Routing and the shared live picture.
 *
 * The track picture is established once here and passed down, rather than each page opening
 * its own connection. One event stream per client means one resume position, and therefore
 * one consistent answer to "what is out there" across every view — two streams would drift
 * and two pages would disagree.
 */

import { useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'

import { AppShell } from '../components/shell/AppShell'
import { useAuth } from '../data/client/auth'
import { useTrackPicture } from '../data/client/useTrackPicture'
import { AlertsPage } from './pages/AlertsPage'
import { LoginPage } from './pages/LoginPage'
import { EventLogPage } from './pages/EventLogPage'
import { OverviewPage } from './pages/OverviewPage'
import { ResponseSystemsPage } from './pages/ResponseSystemsPage'
import { SensorsPage } from './pages/SensorsPage'
import { SystemHealthPage } from './pages/SystemHealthPage'
import { TrackHistoryPage } from './pages/TrackHistoryPage'
import { TracksPage } from './pages/TracksPage'

export function App() {
  const { status } = useAuth()

  // Nothing renders and no stream opens until the session is known. Splitting the console into
  // its own component is what makes that true: a hook cannot be called conditionally, so
  // leaving `useTrackPicture` in this component would open the WebSocket while the login page
  // was on screen — and that connection carries the entire live picture.
  if (status === 'checking') return <Booting />
  if (status !== 'signed-in') return <LoginPage />

  return <Console />
}

/** Deliberately almost empty. The alternative is a flash of the login page on every load for
 *  someone already signed in, which teaches operators that the console logs them out at
 *  random. */
function Booting() {
  return <div className="app-booting">Checking session…</div>
}

function Console() {
  const picture = useTrackPicture()
  const [selectedTrackId, setSelectedTrackId] = useState<string | null>(null)

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
