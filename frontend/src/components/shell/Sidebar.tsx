/**
 * The side rail: every section, numbered, always one click away on a wide screen.
 *
 * On narrow screens it becomes a scrollable strip of the same numbered pages under the top bar.
 * Unavailable entries are marked and are not clickable — see ADR 0011.
 */

import { NavLink } from 'react-router-dom'

import { NAVIGATION } from './navigation'
import './Sidebar.css'

export function Sidebar() {
  return (
    <nav className="sidebar" aria-label="Primary">
      {NAVIGATION.map((section) => (
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
                <span className="sidebar__number numeric">{item.number}</span>
                <span className="sidebar__label">{item.label}</span>
                <span className="sidebar__badge">soon</span>
              </span>
            ) : (
              <NavLink
                key={item.path}
                to={item.path}
                end={item.path === '/'}
                className={({ isActive }) => `sidebar__item ${isActive ? 'is-active' : ''}`}
              >
                <span className="sidebar__number numeric">{item.number}</span>
                <span className="sidebar__label">{item.label}</span>
              </NavLink>
            ),
          )}
        </div>
      ))}
    </nav>
  )
}
