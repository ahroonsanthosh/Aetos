/**
 * Primary navigation.
 *
 * Every section of the finished system appears here, including the ones that are not built.
 * A navigation that hides unimplemented areas makes "not built yet" indistinguishable from
 * "does not exist", and this list is the clearest statement of the system's intended shape.
 * Unavailable entries are marked and are not clickable — see ADR 0011.
 */

import { NavLink } from 'react-router-dom'

import './Sidebar.css'

interface Item {
  label: string
  path: string
  /** Why this is not yet reachable. Present only for entries that are not built. */
  unavailable?: string
}

interface Section {
  title: string
  items: Item[]
}

const SECTIONS: Section[] = [
  {
    title: 'Operations',
    items: [
      { label: 'Overview', path: '/' },
      { label: 'Live Tracks', path: '/tracks' },
      { label: 'Alerts', path: '/alerts' },
    ],
  },
  {
    title: 'Intelligence',
    items: [
      { label: 'Track History', path: '/history' },
      { label: 'Sensor Fusion', path: '/fusion', unavailable: 'Fusion module not implemented' },
      {
        label: 'Classification',
        path: '/classification',
        unavailable: 'Classification module not implemented',
      },
      {
        label: 'Assessments',
        path: '/assessments',
        unavailable: 'Risk module not implemented',
      },
    ],
  },
  {
    title: 'Systems',
    items: [
      { label: 'Sensors', path: '/sensors' },
      { label: 'Response Systems', path: '/response' },
      { label: 'System Health', path: '/health' },
      { label: 'Event Log', path: '/events' },
    ],
  },
]

export function Sidebar() {
  return (
    <nav className="sidebar" aria-label="Primary">
      {SECTIONS.map((section) => (
        <div key={section.title} className="sidebar__section">
          <div className="label sidebar__section-title">{section.title}</div>
          {section.items.map((item) =>
            item.unavailable ? (
              <span
                key={item.path}
                className="sidebar__item sidebar__item--unavailable"
                title={item.unavailable}
                aria-disabled="true"
              >
                {item.label}
                <span className="sidebar__badge">—</span>
              </span>
            ) : (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.path === '/'}
                className={({ isActive }) =>
                  `sidebar__item ${isActive ? 'is-active' : ''}`
                }
              >
                {item.label}
              </NavLink>
            ),
          )}
        </div>
      ))}
    </nav>
  )
}
