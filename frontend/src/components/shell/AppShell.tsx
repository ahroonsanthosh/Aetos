/**
 * Application shell: top bar, sidebar, and the routed content region.
 *
 * Layout priority, per the specification: the operational picture is the last thing to
 * collapse. At narrow widths the sidebar goes first, then secondary panels; the map, track
 * information and alerts survive longest, because they are what the interface is for.
 */

import type { ReactNode } from 'react'

import type { StreamStatus } from '../../data/client/stream'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import './AppShell.css'

interface Props {
  streamStatus: StreamStatus
  trackCount: number
  children: ReactNode
}

export function AppShell({ streamStatus, trackCount, children }: Props) {
  return (
    <div className="shell">
      {/* The operator is read from the session rather than passed in. It used to be the
          string "LOCAL OPERATOR", hardcoded here because no authentication existed; showing a
          name the system had never verified was the honest thing to do then and would be a
          lie now. */}
      <TopBar streamStatus={streamStatus} trackCount={trackCount} />
      <div className="shell__body">
        <Sidebar />
        <main className="shell__content">{children}</main>
      </div>
    </div>
  )
}
