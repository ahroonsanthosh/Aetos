/**
 * The full-screen menu: every section in large numbered type, plus the front page and sign out.
 *
 * Opened from the round button in the top bar. It is the whole navigation on a phone, where the
 * side rail does not fit, and a quicker jump on a wide screen. Escape, the close button, a link,
 * or a route change closes it. Focus moves into it on open and back to the button on close.
 */

import { useEffect, useRef } from 'react'
import { NavLink, useLocation } from 'react-router-dom'

import { NAVIGATION } from './navigation'
import './MenuOverlay.css'

interface Props {
  open: boolean
  onClose: () => void
  onSignOut: () => void
  operator: string | null
}

export function MenuOverlay({ open, onClose, onSignOut, operator }: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const location = useLocation()

  // A navigation closes the menu, whichever way it happened.
  const firstPath = useRef(location.pathname)
  useEffect(() => {
    if (location.pathname !== firstPath.current) onClose()
    firstPath.current = location.pathname
  }, [location.pathname, onClose])

  // Closed, nothing inside can be tabbed to or read out.
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    if (open) root.removeAttribute('inert')
    else root.setAttribute('inert', '')
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    panelRef.current?.querySelector<HTMLElement>('a, button')?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  let index = 0

  return (
    <div ref={rootRef} className={`menu ${open ? 'is-open' : ''}`} aria-hidden={!open}>
      <div className="menu__panel" ref={panelRef} role="dialog" aria-modal="true" aria-label="Menu">
        <div className="menu__top">
          <span className="label">Menu</span>
          <button type="button" className="pill pill--outline" onClick={onClose}>
            Close <span aria-hidden="true">✕</span>
          </button>
        </div>

        <div className="menu__columns">
          {NAVIGATION.map((section) => (
            <section key={section.title} className="menu__section">
              <h2 className="label menu__section-title">{section.title}</h2>
              <ul className="menu__list">
                {section.items.map((item) => {
                  const delay = { animationDelay: `${120 + index++ * 35}ms` }
                  return (
                    <li key={item.path} className="menu__entry" style={delay}>
                      {item.unavailable ? (
                        <span className="menu__link menu__link--unavailable" title={item.unavailable}>
                          <span className="menu__number numeric">{item.number}</span>
                          <span className="menu__text">{item.label}</span>
                          <span className="menu__soon serif">soon</span>
                        </span>
                      ) : (
                        <NavLink
                          to={item.path}
                          end={item.path === '/'}
                          onClick={onClose}
                          className={({ isActive }) => `menu__link ${isActive ? 'is-active' : ''}`}
                        >
                          <span className="menu__number numeric">{item.number}</span>
                          <span className="menu__text">
                            <span className="menu__text-sans">{item.label}</span>
                            <span className="menu__text-serif serif" aria-hidden="true">
                              {item.label}
                            </span>
                          </span>
                          <span className="menu__arrow" aria-hidden="true">↘</span>
                        </NavLink>
                      )}
                    </li>
                  )
                })}
              </ul>
            </section>
          ))}
        </div>

        <div className="menu__foot">
          <NavLink to="/landing" className="pill pill--outline" onClick={onClose}>
            Front page <span className="pill__arrow" aria-hidden="true">↘</span>
          </NavLink>
          {operator && <span className="menu__operator">Signed in as {operator}</span>}
          <button type="button" className="pill pill--ink" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </div>
    </div>
  )
}
