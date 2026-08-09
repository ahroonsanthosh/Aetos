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
      <TopBar
        streamStatus={streamStatus}
        trackCount={trackCount}
        /* No authentication exists (FM-001). The operator identity is the single configured
           local one, shown so actions are attributable in structure — not because anyone has
           been authenticated. */
        operator="LOCAL OPERATOR"
      />
      <div className="shell__body">
        <Sidebar />
        <main className="shell__content">{children}</main>
      </div>
    </div>
  )
}
