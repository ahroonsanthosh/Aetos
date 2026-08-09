/**
 * System health.
 *
 * Reports `GET /system/health` verbatim, including components that are not built. An
 * unimplemented module appears as NOT BUILT rather than being hidden, because a hidden
 * subsystem cannot be told apart from a forgotten one (ADR 0011).
 */

import { useEffect, useState } from 'react'

import { api } from '../../data/client/api'
import type { ComponentState, SystemHealthView } from '../../data/models/api'
import { PageHeader } from './TracksPage'
import './pages.css'
import './SystemHealthPage.css'

const POLL_MS = 10_000

const STATE_LABEL: Record<ComponentState, string> = {
  ok: 'OPERATIONAL',
  degraded: 'DEGRADED',
  unavailable: 'UNAVAILABLE',
  not_built: 'NOT BUILT',
  not_connected: 'NOT CONNECTED',
}

export function SystemHealthPage() {
  const [health, setHealth] = useState<SystemHealthView | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const abort = new AbortController()
    async function poll() {
      try {
        setHealth(await api.health(abort.signal))
        setError(null)
      } catch (cause) {
        if (!abort.signal.aborted) setError(String(cause))
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), POLL_MS)
    return () => {
      abort.abort()
      window.clearInterval(timer)
    }
  }, [])

  return (
    <div className="page">
      <PageHeader title="System Health" />
      <div className="page__body">
        {error && <div className="page__error">{error}</div>}

        {health && (
          <>
            <div className="panel">
              <div className="panel__header">
                <span className="label">Event Log</span>
              </div>
              <div className="panel__body health__metrics">
                <Metric label="Log head" value={String(health.latest_event_seq)} />
                <Metric label="Projection at" value={String(health.projection_seq)} />
                <Metric
                  label="Lag"
                  value={String(health.projection_lag)}
                  note="events behind"
                  tone={health.projection_lag > 50 ? 'warning' : 'nominal'}
                />
              </div>
            </div>

            <div className="panel">
              <div className="panel__header">
                <span className="label">Components</span>
              </div>
              <div className="panel__body">
                {health.components.map((component) => (
                  <div key={component.component} className="health__row">
                    <span className="health__name">{component.component.replace(/_/g, ' ')}</span>
                    <span className={`health__state health__state--${component.state}`}>
                      {STATE_LABEL[component.state]}
                    </span>
                    <span className="health__detail">{component.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Metric({
  label,
  value,
  note,
  tone,
}: {
  label: string
  value: string
  note?: string
  tone?: 'nominal' | 'warning'
}) {
  return (
    <div className="health__metric">
      <span className="label">{label}</span>
      <span className={`numeric health__metric-value ${tone ? `is-${tone}` : ''}`}>{value}</span>
      {note && <span className="health__metric-note">{note}</span>}
    </div>
  )
}
