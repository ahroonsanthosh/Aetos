/**
 * Passing a site from the editor to the command view.
 *
 * The editor and the command interface are separate routes — `/editor` and `/` — so there
 * is no shared React tree to pass a site through as a prop. `sessionStorage` is the
 * simplest thing that works for a same-tab handoff: it survives the navigation, it is
 * cleared on read so a stale site can't reappear after a later reload, and it needs no
 * server, no database, and no account system for a client-only prototype to acquire.
 */

const HANDOFF_KEY = 'aetos-twin-site-handoff';

/** Called by the editor's "Preview in Command" action, just before navigating to `/`. */
export function handOffSiteToCommand(siteJson: string): void {
  if (typeof window === 'undefined') return;
  window.sessionStorage.setItem(HANDOFF_KEY, siteJson);
}

/** Called once by the command view on mount. Returns the handed-off JSON and clears it. */
export function takeHandedOffSite(): string | null {
  if (typeof window === 'undefined') return null;
  const value = window.sessionStorage.getItem(HANDOFF_KEY);
  if (value !== null) window.sessionStorage.removeItem(HANDOFF_KEY);
  return value;
}
