/**
 * Event stream client.
 *
 * The backend's WebSocket resumes from a sequence number, which is what makes a live picture
 * trustworthy rather than merely live. The handover works like this:
 *
 *   1. `GET /tracks` returns the current picture and the `last_applied_seq` it was projected
 *      from.
 *   2. The stream opens at `last_applied_seq + 1`.
 *   3. Events arrive in sequence order and are applied to that picture.
 *
 * There is no gap and no duplicate at the boundary, because `seq` is a total order assigned by
 * a single writer (ADR 0006) rather than a wall-clock timestamp that two sensors might
 * disagree about.
 *
 * The same property makes reconnection sound: on drop, the stream reopens at one past the
 * highest sequence already applied, so nothing that happened during the outage is missed.
 * That is the difference between a live view and a view that is merely usually right.
 */

import { API_PREFIX } from '../models/api'
import type { StreamedEvent } from '../models/api'

export type StreamStatus = 'connecting' | 'live' | 'reconnecting' | 'closed'

export interface StreamHandlers {
  onEvent: (event: StreamedEvent) => void
  onStatusChange: (status: StreamStatus) => void
}

const INITIAL_RECONNECT_DELAY_MS = 500
const MAX_RECONNECT_DELAY_MS = 10_000

/**
 * A resuming connection to the backend event stream.
 *
 * Owns its reconnection policy. Callers get events and a status; they do not manage sockets,
 * because a caller that reconnected on its own terms would eventually reconnect from the
 * wrong sequence number and silently lose events.
 */
export class EventStream {
  private socket: WebSocket | null = null
  private reconnectTimer: number | null = null
  private reconnectDelay = INITIAL_RECONNECT_DELAY_MS
  private closedByCaller = false

  /** Highest sequence applied. The resume point, and the reason no event is seen twice. */
  private lastSeq: number

  constructor(
    fromSeq: number,
    private readonly handlers: StreamHandlers,
  ) {
    this.lastSeq = fromSeq
  }

  start(): void {
    this.closedByCaller = false
    this.open()
  }

  close(): void {
    this.closedByCaller = true
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.socket?.close()
    this.socket = null
    this.handlers.onStatusChange('closed')
  }

  private open(): void {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}${API_PREFIX}/stream?from_seq=${this.lastSeq + 1}`

    this.handlers.onStatusChange(this.reconnectDelay === INITIAL_RECONNECT_DELAY_MS ? 'connecting' : 'reconnecting')

    const socket = new WebSocket(url)
    this.socket = socket

    socket.onopen = () => {
      // Only reset backoff once a connection actually succeeds. Resetting on attempt would
      // turn a server that accepts and immediately drops into a tight reconnect loop.
      this.reconnectDelay = INITIAL_RECONNECT_DELAY_MS
      this.handlers.onStatusChange('live')
    }

    socket.onmessage = (message) => {
      let event: StreamedEvent
      try {
        event = JSON.parse(message.data as string) as StreamedEvent
      } catch {
        // A frame we cannot parse is a contract violation. Dropping it silently would leave a
        // hole in the sequence that nothing detects, so it is skipped without advancing
        // lastSeq — the next reconnect will ask for it again.
        return
      }

      // Guard against replaying anything already applied. The backend should not send it, but
      // an idempotent client is cheap and a double-applied TRACK_LOST is not.
      if (event.seq <= this.lastSeq) return

      this.lastSeq = event.seq
      this.handlers.onEvent(event)
    }

    socket.onclose = () => {
      this.socket = null
      if (this.closedByCaller) return
      this.scheduleReconnect()
    }

    socket.onerror = () => {
      // `onclose` always follows, and that is where reconnection is handled. Acting here too
      // would schedule two reconnects for one failure.
      socket.close()
    }
  }

  private scheduleReconnect(): void {
    this.handlers.onStatusChange('reconnecting')

    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.open()
    }, this.reconnectDelay)

    // Exponential backoff, capped. An operations room does not want a browser hammering a
    // backend that is already struggling.
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_RECONNECT_DELAY_MS)
  }
}
