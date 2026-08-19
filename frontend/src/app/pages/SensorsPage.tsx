/**
 * Sensor management.
 *
 * Configured placement, and — where anything is watching — the condition the system health
 * watchdog has concluded. Both, together, because neither is the whole answer: placement says
 * what the site declares, condition says what is actually arriving from it, and a sensor
 * present in one and not the other is a real situation to show rather than an error to hide.
 *
 * The condition column is blank for a sensor nothing has assessed, and says so in as many
 * words. That is the load-bearing distinction: "no watchdog is looking at this" and "a
 * watchdog looked and found it healthy" render very differently on purpose (ADR 0011). This
 * page said the watchdog was not implemented for as long as that was true; it reads the
 * backend for the answer now, so the next module to land cannot leave it asserting otherwise.
 *
 * Nothing here is specific to any sensor type or manufacturer. The page renders whatever the
 * site configuration defines and whatever the backend reports about it.
 */

import type { SensorConditionView, SensorView } from '../../data/models/api'
import { PageHeader } from './TracksPage'
import { useSensors } from './OverviewPage'
import './pages.css'
import './tables.css'

const CONDITION_LABEL: Record<SensorConditionView['condition'], string> = {
  not_started: 'NOT STARTED',
  ok: 'OK',
  degraded: 'DEGRADED',
  silent: 'SILENT',
  offline: 'OFFLINE',
}

/** Deliberately colourless for `not_started`: nothing has gone wrong, and nothing is confirmed
 *  right either. Colouring it green would claim the first; red would claim the second. */
const CONDITION_CLASS: Record<SensorConditionView['condition'], string> = {
  not_started: 'condition--inert',
  ok: 'condition--ok',
  degraded: 'condition--degraded',
  silent: 'condition--silent',
  offline: 'condition--offline',
}

export function SensorsPage() {
  const sensors = useSensors()
  const assessed = sensors.filter((s) => s.condition !== null).length

  return (
    <div className="page">
      <PageHeader title="Sensors" subtitle={`${sensors.length} configured`} />
      <div className="page__body">
        <p className="page__note">
          {assessed === 0
            ? 'Configured placement only. No sensor is currently being watched, so no condition ' +
              'is shown — an unwatched sensor is not a healthy one, and this page will not ' +
              'render it as though it were.'
            : `Condition is the system health watchdog's, refreshed each cycle. ${assessed} of ` +
              `${sensors.length} configured sensors are being watched; the rest are declared in ` +
              'site configuration with nothing attached behind them, and are shown as not ' +
              'assessed rather than as healthy.'}
        </p>

        <div className="panel">
          <table className="sensor-table">
            <thead>
              <tr>
                <th className="label">Sensor</th>
                <th className="label">Type</th>
                <th className="label">Condition</th>
                <th className="label">Detections</th>
                <th className="label">Last seen</th>
                <th className="label">Registry key</th>
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
                  <ConditionCells sensor={sensor} />
                  <td className="numeric">{sensor.registry_key}</td>
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
  )
}

/** The three watchdog-derived cells, kept together so the unassessed case is written once.
 *
 *  All three go blank at the same time, because the counts come from the same observation as
 *  the condition. Showing a detection count of zero next to an empty condition would read as a
 *  sensor that has produced nothing, when in fact nobody counted. */
function ConditionCells({ sensor }: { sensor: SensorView }) {
  const condition = sensor.condition
  if (condition === null) {
    return (
      <>
        <td className="condition--unassessed" title="No watchdog is assessing this sensor.">
          NOT ASSESSED
        </td>
        <td className="numeric">—</td>
        <td className="numeric">—</td>
      </>
    )
  }

  return (
    <>
      <td className={CONDITION_CLASS[condition.condition]} title={condition.reason}>
        {CONDITION_LABEL[condition.condition]}
      </td>
      <td className="numeric">{condition.detections_emitted.toLocaleString()}</td>
      <td className="numeric" title={condition.last_detection_time ?? 'Never'}>
        {formatSilence(condition)}
      </td>
    </>
  )
}

/** How long since this sensor last produced, in the units an operator reads at a glance.
 *
 *  The detection count is checked first, and that is not a tidiness detail. `silent_for_s`
 *  falls back to time-since-started for a sensor that has never produced anything, so a sensor
 *  with nothing behind it rendered as "72s ago" next to a count of zero — two cells of the same
 *  row contradicting each other, with the wrong one being the reassuring one. A sensor that has
 *  emitted nothing has no last-seen time at any age.
 *
 *  "Never" rather than a dash, because a dash reads as missing information and this is
 *  information: an operator seeing it knows the sensor has produced nothing since startup. */
function formatSilence(condition: SensorConditionView): string {
  if (condition.detections_emitted === 0) return 'Never'
  if (condition.silent_for_s === null) return '—'
  const seconds = condition.silent_for_s
  if (seconds < 90) return `${seconds.toFixed(0)}s ago`
  return `${(seconds / 60).toFixed(1)}m ago`
}
