/**
 * Response Systems — architecture and interface only. Nothing here does anything.
 *
 * This page exists so that adding response integration later is an extension of the
 * architecture rather than a redesign of it. It contains no control, no command, and no code
 * path capable of reaching an external system. Every card is permanently NOT CONNECTED.
 *
 * The page is deliberately plain. A response layer rendered with more visual weight than the
 * operational picture would misrepresent what Aetos is: a sensor-fusion and decision-support
 * product whose output is advice to a human.
 *
 * See `response/README.md` at the repository root for the authorisation, safety and audit
 * work required before any of this becomes real.
 */

import { PageHeader } from './TracksPage'
import './pages.css'
import './ResponseSystemsPage.css'

/** Data shape only. `status` has exactly one permitted value in this build. */
interface ResponseSystem {
  id: string
  name: string
  type: string
  status: 'not_connected'
  connectionStatus: string
  capabilities: string[]
  lastHeartbeat: number | null
}

/**
 * Placeholder entries. Deliberately generic: no manufacturer, no product, no capability that
 * describes a physical effect. These describe an integration surface, not equipment.
 */
const PLACEHOLDERS: ResponseSystem[] = [
  {
    id: 'RS-01',
    name: 'Response System 01',
    type: 'Future response system',
    status: 'not_connected',
    connectionStatus: 'Not configured',
    capabilities: ['Future integration', 'External system interface', 'Status monitoring'],
    lastHeartbeat: null,
  },
  {
    id: 'RS-02',
    name: 'Response System 02',
    type: 'Future response system',
    status: 'not_connected',
    connectionStatus: 'Not configured',
    capabilities: ['Future integration', 'External system interface', 'Status monitoring'],
    lastHeartbeat: null,
  },
]

export function ResponseSystemsPage() {
  return (
    <div className="page">
      <PageHeader title="Response Systems" subtitle="Not implemented" />
      <div className="page__body">
        <p className="page__note">
          Aetos ends at a recommendation presented to a human operator. This page is an
          architectural placeholder for a future response-integration layer; it has no
          functional capability, and this build contains no code path able to activate an
          external system.
        </p>
        <p className="page__note">
          The pipeline is <strong>detection → assessment → human decision</strong>. Response
          integration sits beyond that boundary and requires its own authorisation, safety and
          audit design pass before any of it is built.
        </p>

        <div className="response-grid">
          {PLACEHOLDERS.map((system) => (
            <article key={system.id} className="panel response-card">
              <div className="panel__header">
                <span className="response-card__name">{system.name}</span>
                <span className="response-card__status">NOT CONNECTED</span>
              </div>
              <div className="panel__body">
                <Row label="Type" value={system.type} />
                <Row label="Connection" value={system.connectionStatus} />
                <Row
                  label="Last heartbeat"
                  value={system.lastHeartbeat === null ? '—' : String(system.lastHeartbeat)}
                />
                <div className="response-card__capabilities">
                  <span className="label">Capabilities</span>
                  <ul>
                    {system.capabilities.map((capability) => (
                      <li key={capability}>{capability}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </article>
          ))}
        </div>
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="response-card__row">
      <span className="label">{label}</span>
      <span>{value}</span>
    </div>
  )
}
