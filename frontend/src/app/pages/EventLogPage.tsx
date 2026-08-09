/**
 * Event log: a read view onto the backend's append-only store.
 *
 * The log is the source of truth (ADR 0006) and this is a window onto it, never a second
 * record. Events are shown newest first with their sequence number, which is the ordering
 * authority — timestamps come from sensors whose clocks may disagree.
 *
 * This view is populated from the live stream rather than fetched, so it shows what has
 * happened since the page was opened. A full historical query needs a paginated events
 * endpoint, which the API does not yet expose.
 */

import { useEffect, useRef, useState } from 'react'

import { api } from '../../data/client/api'
import { EventStream } from '../../data/client/stream'
import type { StreamedEvent } from '../../data/models/api'
import { PageHeader } from './TracksPage'
import './pages.css'
import './tables.css'

const MAX_ROWS = 500

export function EventLogPage() {
  const [events, setEvents] = useState<StreamedEvent[]>([])
  const [filter, setFilter] = useState('')
  const streamRef = useRef<EventStream | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    let cancelled = false

    async function open() {
      const health = await api.health(abort.signal)
      if (cancelled) return

      // Start a little behind the head so the view is not empty on arrival.
      const from = Math.max(0, health.latest_event_seq - 100)
      const stream = new EventStream(from, {
        onEvent: (event) =>
          // Newest first, bounded. An unbounded log view in a long-running operations room
          // eventually consumes the tab.
          setEvents((current) => [event, ...current].slice(0, MAX_ROWS)),
        onStatusChange: () => undefined,
      })
      streamRef.current = stream
      stream.start()
    }

    void open().catch(() => undefined)

    return () => {
      cancelled = true
      abort.abort()
      streamRef.current?.close()
    }
  }, [])

  const needle = filter.trim().toLowerCase()
  const visible = needle
    ? events.filter(
        (e) =>
          e.event_type.toLowerCase().includes(needle) ||
          e.source.toLowerCase().includes(needle) ||
          (e.track_id ?? '').toLowerCase().includes(needle),
      )
    : events

  return (
    <div className="page">
      <PageHeader title="Event Log" subtitle={`${visible.length} shown`} />
      <div className="page__body">
        <input
          className="event-filter"
          type="search"
          placeholder="Filter by event type, source or track"
          value={filter}
          onChange={(changeEvent) => setFilter(changeEvent.target.value)}
        />

        <div className="panel">
          <table className="event-table">
            <thead>
              <tr>
                <th className="label">Seq</th>
                <th className="label">Time</th>
                <th className="label">Event</th>
                <th className="label">Source</th>
                <th className="label">Track</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((event) => (
                <tr key={event.seq}>
                  <td className="numeric">{event.seq}</td>
                  <td className="numeric">{event.timestamp.slice(11, 23)}</td>
                  <td className="numeric event-table__type">{event.event_type}</td>
                  <td>{event.source}</td>
                  <td className="numeric">{event.track_id ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!visible.length && (
            <div className="empty-state">
              <div className="empty-state__title">No events</div>
              <p className="empty-state__body">
                This view shows events as they arrive. Start a scenario to generate activity.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
