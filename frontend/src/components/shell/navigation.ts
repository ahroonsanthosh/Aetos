/**
 * Every section of the console, in one list, numbered.
 *
 * Read by both the side rail and the full-screen menu, so the two can never disagree about what
 * exists. Entries that are not built yet are listed and marked rather than hidden: a navigation
 * that hides unimplemented areas makes "not built yet" indistinguishable from "does not exist"
 * (ADR 0011).
 */

export interface NavItem {
  /** Two-digit position, shown before the label. */
  number: string
  label: string
  path: string
  /** Why this is not yet reachable. Present only for entries that are not built. */
  unavailable?: string
}

export interface NavSection {
  title: string
  items: NavItem[]
}

const RAW: { title: string; items: Omit<NavItem, 'number'>[] }[] = [
  {
    title: 'Operations',
    items: [
      { label: 'Overview', path: '/' },
      { label: 'Live Tracks', path: '/tracks' },
      { label: 'Alerts', path: '/alerts' },
      { label: 'Digital Twin', path: '/digital-twin' },
    ],
  },
  {
    title: 'Intelligence',
    items: [
      { label: 'Track History', path: '/history' },
      {
        label: 'Sensor Fusion',
        path: '/fusion',
        unavailable: 'Fusion runs in the pipeline; no dedicated page yet',
      },
      {
        label: 'Classification',
        path: '/classification',
        unavailable: 'No dedicated page yet — classification is shown per track on Live Tracks',
      },
      {
        label: 'Assessments',
        path: '/assessments',
        unavailable: 'No dedicated page yet — risk scores and reasons are shown per track on Live Tracks',
      },
    ],
  },
  {
    title: 'Systems',
    items: [
      { label: 'Sensors', path: '/sensors' },
      { label: 'Camera', path: '/camera' },
      { label: 'Response Systems', path: '/response' },
      { label: 'System Health', path: '/health' },
      { label: 'Event Log', path: '/events' },
    ],
  },
]

let counter = 0
export const NAVIGATION: NavSection[] = RAW.map((section) => ({
  title: section.title,
  items: section.items.map((item) => ({
    ...item,
    number: String(++counter).padStart(2, '0'),
  })),
}))
