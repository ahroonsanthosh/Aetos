/**
 * Alert feed.
 *
 * Shows open alerts — including acknowledged ones, which are still open. Acknowledgement
 * records that a human saw the alert, not that the aircraft went away, and a page that hid
 * acknowledged alerts would let an operator clear their own display of a live incursion.
 *
 * The empty state distinguishes "nothing is wrong" from "nothing is watching" using the
 * response's `assessing` flag rather than inferring it from an empty array. Those two are the
 * same shape on the wire and opposite in meaning (ADR 0011). An earlier version of this page
 * had no such flag to consult and hardcoded the pessimistic reading, which then stayed on
 * screen after alerting was built.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../../data/client/api'
import type { AlertListResponse, AlertView } from '../../data/models/api'
import { PageHeader } from './TracksPage'
import './pages.css'
import './tables.css'

const REFRESH_MS = 2000

export function AlertsPage() {
  const [feed, setFeed] = useState<AlertListResponse | null>(null)
  const [failed, setFailed] = useState(false)
  const [pending, setPending] = useState<string | null>(null)

  // Held in a ref so the polling effect does not restart on every tick.
  const acknowledging = useRef(false)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setFeed(await api.alerts(signal))
      setFailed(false)
    } catch {
      if (!signal?.aborted) setFailed(true)
    }
  }, [])

  useEffect(() => {
    const abort = new AbortController()
    void load(abort.signal)
    const timer = window.setInterval(() => {
      if (!acknowledging.current) void load(abort.signal)
    }, REFRESH_MS)

    return () => {
      abort.abort()
      window.clearInterval(timer)
    }
  }, [load])

  const acknowledge = useCallback(
    async (alertId: string) => {
      acknowledging.current = true
      setPending(alertId)
      try {
        await api.acknowledgeAlert(alertId)
        await load()
      } catch {
        setFailed(true)
      } finally {
        setPending(null)
        acknowledging.current = false
      }
    },
    [load],
  )

  const alerts = feed?.alerts ?? []
  const subtitle = feed ? `${feed.count} open` : undefined

  return (
    <div className="page">
      <PageHeader title="Alerts" subtitle={subtitle} />
      <div className="page__body">
        {failed && (
          <div className="panel alerts__banner alerts__banner--error">
            The alert feed could not be read. What is shown below may be out of date.
          </div>
        )}

        {alerts.length ? (
          <div className="panel alerts">
            {alerts.map((alert) => (
              <AlertRow
                key={alert.alert_id}
                alert={alert}
                busy={pending === alert.alert_id}
                onAcknowledge={acknowledge}
              />
            ))}
          </div>
        ) : (
          <div className="panel">
            <EmptyState feed={feed} />
          </div>
        )}
      </div>
    </div>
  )
}

function EmptyState({ feed }: { feed: AlertListResponse | null }) {
  if (feed === null) {
    return (
      <div className="empty-state">
        <div className="empty-state__title">Loading</div>
      </div>
    )
  }

  if (!feed.assessing) {
    return (
      <div className="empty-state">
        <div className="empty-state__title">Alerting is not running</div>
        <p className="empty-state__body">
          No alerting service is attached to this deployment, so nothing is evaluating tracks
          against alert thresholds. This is <strong>not</strong> an &ldquo;all clear&rdquo; —
          judge the current picture from the track list.
        </p>
      </div>
    )
  }

  return (
    <div className="empty-state">
      <div className="empty-state__title">No open alerts</div>
      <p className="empty-state__body">
        Alerting is running and no track currently meets the threshold. Tracks below it are
        still assessed and scored — the track list shows every one, with its risk level.
      </p>
    </div>
  )
}

function AlertRow({
  alert,
  busy,
  onAcknowledge,
}: {
  alert: AlertView
  busy: boolean
  onAcknowledge: (alertId: string) => void
}) {
  const acknowledged = alert.state === 'acknowledged'

  return (
    <div className={`alert-card alert-card--${alert.peak_level}`}>
      <div className="alert-card__head">
        <span className={`alert-card__level alert-card__level--${alert.peak_level}`}>
          {alert.peak_level.toUpperCase()}
        </span>
        <span className="numeric alert-card__track">{alert.track_id}</span>
        <span className="numeric alert-card__score">{alert.score}</span>
        <span className="numeric alert-card__time">
          {new Date(alert.raised_at).toISOString().slice(11, 19)}Z
        </span>

        {/* Only shown once the current level has actually fallen below the peak. Displaying
            "now MEDIUM" on an alert still sitting at its peak would read as de-escalation
            that has not happened. */}
        {alert.level !== alert.peak_level && (
          <span className="alert-card__now">now {alert.level.toUpperCase()}</span>
        )}

        {alert.escalated_at && <span className="alert-card__escalated">ESCALATED</span>}

        <span className="alert-card__actions">
          {acknowledged ? (
            <span
              className="alert-card__acknowledged"
              title={`Acknowledged by ${alert.acknowledged_by ?? 'an operator'}. The alert is still open.`}
            >
              ACKNOWLEDGED
            </span>
          ) : (
            <button
              type="button"
              className="alert-card__ack-button"
              disabled={busy}
              onClick={() => onAcknowledge(alert.alert_id)}
              title="Record that you have seen this alert. It does not clear it."
            >
              {busy ? '…' : 'Acknowledge'}
            </button>
          )}
        </span>
      </div>

      {/* Risk's own sentences, unedited. An alert that cannot say why it fired leaves an
          operator with obey or dismiss. */}
      <ul className="alert-card__reasons">
        {alert.reasons.map((reason, index) => (
          <li key={alert.codes[index] ?? reason}>{reason}</li>
        ))}
      </ul>
    </div>
  )
}
