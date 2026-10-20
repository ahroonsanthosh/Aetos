/**
 * Sensor management.
 *
 * Shows configured placement. Runtime status — in contact, quiet, offline — is judged by the
 * watchdog (`backend/system_health`) and summarised on the System Health page; it is not yet
 * broken out per sensor here. Those columns are absent rather than filled with a plausible green
 * light: the backend's own sensors endpoint takes the same position, on the grounds that a green
 * light nothing computes is worse than no light.
 *
 * Nothing here is specific to any sensor type or manufacturer. The page renders whatever the
 * site configuration defines.
 */

import { PageHeader } from './TracksPage'
import { useSensors } from './OverviewPage'
import './pages.css'
import './tables.css'

export function SensorsPage() {
  const sensors = useSensors()

  return (
    <div className="page">
      <PageHeader title="Sensors" subtitle={`${sensors.length} configured`} />
      <div className="page__body">
        <p className="page__note">
          Configured placement only. Runtime status, update rate and last-packet age require the
          system health watchdog, which is not implemented — so those fields are not shown
          rather than being displayed as unknown-but-fine.
        </p>

        <div className="panel">
          <div className="table-scroll">
            <table className="sensor-table">
              <thead>
                <tr>
                  <th className="label">Sensor</th>
                  <th className="label">Type</th>
                  <th className="label">Source</th>
                  <th className="label">Position</th>
                  <th className="label">Height</th>
                  <th className="label">Boresight</th>
                  <th className="label">Configured</th>
                </tr>
              </thead>
              <tbody>
                {sensors.map((sensor) => (
                  <tr key={sensor.sensor_id}>
                    <td className="numeric">{sensor.sensor_id}</td>
                    <td>{sensor.sensor_type ? sensor.sensor_type.replace(/_/g, ' ') : '—'}</td>
                    <td className="numeric">
                      {sensor.hardware_driver
                        ? `hardware · ${sensor.hardware_driver}`
                        : sensor.registry_key
                          ? `simulated · ${sensor.registry_key}`
                          : '—'}
                    </td>
                    <td className="numeric">
                      {sensor.position.latitude.toFixed(5)}, {sensor.position.longitude.toFixed(5)}
                    </td>
                    <td className="numeric">{sensor.height_m.toFixed(1)} m</td>
                    <td className="numeric">{sensor.boresight_deg.toFixed(0)}°</td>
                    <td className={sensor.enabled ? 'is-enabled' : 'is-disabled'}>
                      {sensor.enabled ? 'ENABLED' : 'DISABLED'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}
