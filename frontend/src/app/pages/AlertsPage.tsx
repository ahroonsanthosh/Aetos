/**
 * Alert feed.
 *
 * Alert generation is not implemented in the backend, so this feed is empty and says why. An
 * empty feed that looked like "all clear" would be the more dangerous reading (ADR 0011).
 */

import { useEffect, useState } from 'react'

import { api } from '../../data/client/api'
import type { AlertView } from '../../data/models/api'
import { PageHeader } from './TracksPage'
import './pages.css'
import './tables.css'

export function AlertsPage() {
  const [alerts, setAlerts] = useState<AlertView[] | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    api
      .alerts(abort.signal)
      .then((response) => setAlerts(response.alerts))
      .catch(() => setAlerts([]))
    return () => abort.abort()
  }, [])

  return (
    <div className="page">
      <PageHeader title="Alerts" subtitle={alerts ? `${alerts.length} active` : undefined} />
      <div className="page__body">
        {alerts?.length ? (
          <div className="panel">
            {alerts.map((alert) => (
              <div key={alert.alert_id} className="alert-row">
                <span className="numeric alert-row__time">
                  {new Date(alert.created_at).toISOString().slice(11, 19)}Z
                </span>
                <span className="numeric alert-row__track">{alert.track_id ?? '—'}</span>
                <span className="alert-row__source">{alert.source}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="panel">
            <div className="empty-state">
              <div className="empty-state__title">No alerts</div>
              <p className="empty-state__body">
                Alert generation is not implemented in this build. This is not an
                &ldquo;all clear&rdquo; — nothing is currently evaluating tracks against alert
                criteria, because the risk assessment module has not been built.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
