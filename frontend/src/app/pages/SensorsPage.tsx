/**
 * Sensor management.
 *
 * Shows configured placement. Runtime status — in contact, quiet, offline — is judged by the
 * watchdog (`backend/system_health`) and shown per sensor on the System Health page, which this
 * page links to rather than repeating. A status column here would be a second copy that could
 * disagree with the first.
 *
 * Nothing here is specific to any sensor type or manufacturer. The page renders whatever the
 * site configuration defines.
 */

import { Link } from 'react-router-dom'

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
          Configured placement only. Whether each sensor is in contact, quiet or offline is
          judged by the system health watchdog and shown on{' '}
          <Link to="/health">System Health</Link>. A browser camera, if one is running, is on{' '}
          <Link to="/camera">Camera</Link>.
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
