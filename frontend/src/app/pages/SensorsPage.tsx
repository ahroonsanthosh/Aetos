/**
 * Sensor management.
 *
 * Shows configured placement and whether each placement is actually running, which is what the
 * backend can genuinely report. Runtime status of a *running* sensor — connected or silent,
 * update rate, last packet age, tracks contributed — requires the system_health watchdog, which
 * is not built. Those columns are absent rather than filled with a plausible green light: the
 * backend's own sensors endpoint takes the same position, on the grounds that a green light
 * nothing computes is worse than no light.
 *
 * Two columns here exist because their absence was itself misleading.
 *
 * **Source** says whether a sensor is synthetic or real. It is read from the backend's sensor
 * type, never inferred here — the interface must show simulated-versus-real unmissably, and a
 * frontend that decided this for itself could present synthetic data as real by editing one
 * line. An unknown type renders as unknown; it is never resolved to "real", because that is the
 * worse of the two errors to make by default.
 *
 * **Running** says whether an adapter was built and started. A site fitted with real sensors and
 * running a training scenario legitimately starts fewer adapters than it defines, and without
 * this column the page silently disagreed with the site document: a deliberate coverage gap
 * looked exactly like a smaller site.
 *
 * Nothing here is specific to any sensor type or manufacturer. The page renders whatever the
 * site configuration defines.
 */

import { PageHeader } from './TracksPage'
import { useSensors } from './OverviewPage'
import type { SensorView } from '../../data/models/api'
import './pages.css'
import './tables.css'

function sourceLabel(sensor: SensorView): { text: string; className: string } {
  if (sensor.simulated === null) return { text: 'UNKNOWN', className: 'is-unknown' }
  return sensor.simulated
    ? { text: 'SIMULATED', className: 'is-simulated' }
    : { text: 'REAL', className: 'is-real' }
}

function runningLabel(sensor: SensorView): { text: string; className: string } {
  if (sensor.started === null) return { text: 'UNKNOWN', className: 'is-unknown' }
  return sensor.started
    ? { text: 'RUNNING', className: 'is-enabled' }
    : { text: 'NOT STARTED', className: 'is-disabled' }
}

export function SensorsPage() {
  const sensors = useSensors()
  const running = sensors.filter((s) => s.started).length
  const notStarted = sensors.filter((s) => s.started === false)

  return (
    <div className="page">
      <PageHeader
        title="Sensors"
        subtitle={`${sensors.length} configured · ${running} running`}
      />
      <div className="page__body">
        <p className="page__note">
          Configured placement, and whether an adapter was started for it. Whether a running
          sensor has gone silent, its update rate and its last-packet age require the system
          health watchdog, which is not implemented — so those fields are not shown rather than
          being displayed as unknown-but-fine.
        </p>

        {notStarted.length > 0 && (
          <p className="page__note page__note--warning">
            {notStarted.length} configured sensor
            {notStarted.length === 1 ? ' is' : 's are'} not running. Coverage is smaller than the
            site configuration describes.
          </p>
        )}

        <div className="panel">
          <table className="sensor-table">
            <thead>
              <tr>
                <th className="label">Sensor</th>
                <th className="label">Source</th>
                <th className="label">Type</th>
                <th className="label">Registry key</th>
                <th className="label">Position</th>
                <th className="label">Height</th>
                <th className="label">Boresight</th>
                <th className="label">Configured</th>
                <th className="label">Running</th>
              </tr>
            </thead>
            <tbody>
              {sensors.map((sensor) => {
                const source = sourceLabel(sensor)
                const state = runningLabel(sensor)
                return (
                  <tr key={sensor.sensor_id}>
                    <td className="numeric">{sensor.sensor_id}</td>
                    <td className={source.className}>{source.text}</td>
                    <td>{sensor.sensor_type ? sensor.sensor_type.replace(/_/g, ' ') : '—'}</td>
                    <td className="numeric">{sensor.registry_key}</td>
                    <td className="numeric">
                      {sensor.position.latitude.toFixed(5)}, {sensor.position.longitude.toFixed(5)}
                    </td>
                    <td className="numeric">{sensor.height_m.toFixed(1)} m</td>
                    <td className="numeric">{sensor.boresight_deg.toFixed(0)}°</td>
                    <td className={sensor.enabled ? 'is-enabled' : 'is-disabled'}>
                      {sensor.enabled ? 'ENABLED' : 'DISABLED'}
                    </td>
                    <td className={state.className} title={sensor.not_started_reason ?? undefined}>
                      {state.text}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {notStarted.length > 0 && (
          <div className="panel">
            <h3 className="panel__title">Why they are not running</h3>
            <table className="sensor-table">
              <thead>
                <tr>
                  <th className="label">Sensor</th>
                  <th className="label">Reason</th>
                </tr>
              </thead>
              <tbody>
                {notStarted.map((sensor) => (
                  <tr key={sensor.sensor_id}>
                    <td className="numeric">{sensor.sensor_id}</td>
                    <td>{sensor.not_started_reason ?? 'no reason reported'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
