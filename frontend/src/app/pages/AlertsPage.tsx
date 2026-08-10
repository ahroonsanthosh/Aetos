/**
 * Alert feed.
 *
 * Alert generation is not implemented in the backend, so this feed is empty and says why. An
 * empty feed that looked like "all clear" would be the more dangerous reading (ADR 0011).
 *
 * The gap is specifically *promotion*, not assessment: risk scores every track and emits
 * `RISK_CHANGED`, but nothing turns those into an alert with an identity and a lifecycle.
 * Say that precisely. An earlier version of this page blamed the missing feed on risk
 * assessment "not having been built", which stayed on screen after risk was built and told
 * the operator nothing was evaluating tracks while the Tracks page showed live scores beside
 * it. A stale disclaimer is worse than no disclaimer: it spends the credibility that makes
 * the honest ones worth reading.
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
                &ldquo;all clear&rdquo;.
              </p>
              <p className="empty-state__body">
                Risk assessment <em>is</em> running: every track carries a score, a level and
                reason codes, shown on the Tracks page and in the assessment panel. What is
                missing is the step after it — nothing promotes a risk level into an alert
                that persists, can be acknowledged, and stays put once raised. Judge the
                current picture from the track list, not from this page.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
