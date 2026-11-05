/**
 * The menu drawer: every section in the same numbered list as the side rail, sliding in from
 * the right, with a filter to jump straight to a page.
 *
 * Opened from the round button in the top bar. It is the whole navigation on a phone, where the
 * rail does not fit. Type to filter; Enter opens the first match. Escape, the close button, the
 * backdrop, a link or a route change closes it. Focus moves to the filter on open.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'

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
  const filterRef = useRef<HTMLInputElement>(null)
  const location = useLocation()
  const navigate = useNavigate()
  const [query, setQuery] = useState('')

  // A navigation closes the drawer, whichever way it happened.
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
    if (!open) {
      setQuery('')
      return
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    const focus = window.setTimeout(() => filterRef.current?.focus(), 60)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.clearTimeout(focus)
    }
  }, [open, onClose])

  const sections = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return NAVIGATION
    return NAVIGATION.map((section) => ({
      ...section,
      items: section.items.filter(
        (item) => item.label.toLowerCase().includes(q) || item.number === q.padStart(2, '0'),
      ),
    })).filter((section) => section.items.length > 0)
  }, [query])

  const firstMatch = sections.flatMap((s) => s.items).find((item) => !item.unavailable)

  return (
    <div ref={rootRef} className={`drawer ${open ? 'is-open' : ''}`} aria-hidden={!open}>
      <button type="button" className="drawer__backdrop" aria-label="Close menu" onClick={onClose} />
      <aside className="drawer__panel" role="dialog" aria-modal="true" aria-label="Menu">
        <div className="drawer__top">
          <span className="label">Navigate</span>
          <button type="button" className="drawer__close" onClick={onClose} aria-label="Close menu">
            <span aria-hidden="true">×</span>
          </button>
        </div>

        <form
          className="drawer__filter"
          onSubmit={(event) => {
            event.preventDefault()
            if (firstMatch) navigate(firstMatch.path)
          }}
        >
          <input
            ref={filterRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Jump to a page or number"
            aria-label="Jump to a page"
          />
          <kbd className="numeric">↵</kbd>
        </form>

        <nav className="drawer__nav" aria-label="All pages">
          {sections.map((section) => (
            <div key={section.title} className="drawer__section">
              <div className="label drawer__section-title">{section.title}</div>
              {section.items.map((item) =>
                item.unavailable ? (
                  <span
                    key={item.path}
                    className="drawer__item drawer__item--unavailable"
                    title={item.unavailable}
                    aria-disabled="true"
                  >
                    <span className="drawer__number numeric">{item.number}</span>
                    <span className="drawer__label">{item.label}</span>
                    <span className="drawer__soon">soon</span>
                  </span>
                ) : (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    end={item.path === '/'}
                    onClick={onClose}
                    className={({ isActive }) =>
                      `drawer__item ${isActive ? 'is-active' : ''} ${
                        firstMatch && query && item.path === firstMatch.path ? 'is-match' : ''
                      }`
                    }
                  >
                    <span className="drawer__number numeric">{item.number}</span>
                    <span className="drawer__label">{item.label}</span>
                  </NavLink>
                ),
              )}
            </div>
          ))}
          {sections.length === 0 && <p className="drawer__empty">No page matches “{query}”.</p>}
        </nav>

        <div className="drawer__foot">
          {operator && <span className="drawer__operator">{operator}</span>}
          <div className="drawer__actions">
            <NavLink to="/landing" className="pill pill--outline" onClick={onClose}>
              Front page
            </NavLink>
            <button type="button" className="pill" onClick={onSignOut}>
              Sign out
            </button>
          </div>
        </div>
      </aside>
    </div>
  )
}
