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
import { useTrackPicture } from '../data/client/useTrackPicture'
import { AlertsPage } from './pages/AlertsPage'
import { EventLogPage } from './pages/EventLogPage'
import { OverviewPage } from './pages/OverviewPage'
import { ResponseSystemsPage } from './pages/ResponseSystemsPage'
import { SensorsPage } from './pages/SensorsPage'
import { SystemHealthPage } from './pages/SystemHealthPage'
import { TracksPage } from './pages/TracksPage'

export function App() {
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
