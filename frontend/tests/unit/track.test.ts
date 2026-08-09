/**
 * Presentation model: state mapping, and the honesty rules that must not regress.
 */

import { describe, expect, it } from 'vitest'

import type { TrackHistoryPointView, TrackView } from '../../src/data/models/api'
import { LocalFrame } from '../../src/data/models/geo'
import { toDisplayTrack, toHistoryPoint } from '../../src/data/models/track'

const SITE = { latitude: 53.35, longitude: -6.26, altitude_m: 0 }
const frame = new LocalFrame(SITE)

function view(overrides: Partial<TrackView> = {}): TrackView {
  return {
    track_id: 'TRK-00001',
    state: 'confirmed',
    displayable: true,
    position: { latitude: 53.36, longitude: -6.25, altitude_m: 120 },
    kinematics: { speed_mps: 20, heading_deg: 90, vertical_rate_mps: 0 },
    classification: { label: 'unknown', confidence: 0 },
    threat: { risk_score: 0, risk_level: 'none', reasons: [], recommendation: 'MONITOR' },
    tracking: {
      first_seen: '2026-08-08T12:00:00Z',
      last_seen: '2026-08-08T12:01:00Z',
      detection_count: 12,
      missed_updates: 0,
      coasting: false,
      track_confidence: 0.9,
      contributing_sensors: ['radar_north'],
    },
    updated_at: '2026-08-08T12:01:00Z',
    last_applied_seq: 42,
    ...overrides,
  }
}

describe('track state vocabulary', () => {
  it('maps each backend state to exactly one display state', () => {
    const states = (['new', 'tentative', 'confirmed', 'stale', 'lost'] as const).map(
      (state) => toDisplayTrack(view({ state }), frame).state,
    )

    expect(states).toEqual(['DETECTED', 'ACQUIRING', 'TRACKING', 'STALE', 'LOST'])
  })

  it('invents no state the backend cannot produce', () => {
    // The specification lists MONITORED and DISCARDED. The backend has no such states, and a
    // state the system cannot enter must not appear in an interface an operator reads as a
    // description of reality.
    const produced = new Set(
      (['new', 'tentative', 'confirmed', 'stale', 'lost'] as const).map(
        (state) => toDisplayTrack(view({ state }), frame).state,
      ),
    )

    expect(produced.has('MONITORED' as never)).toBe(false)
    expect(produced.has('DISCARDED' as never)).toBe(false)
  })
})

describe('assessment availability', () => {
  it('reports an unassessed track as unassessed', () => {
    // Rendering the default as a finding would present an unassessed track as
    // assessed-and-benign, which is the more dangerous mistake.
    expect(toDisplayTrack(view(), frame).assessment.available).toBe(false)
  })

  it('becomes available as soon as the backend says anything real', () => {
    const classified = toDisplayTrack(
      view({ classification: { label: 'small_uas', confidence: 0.8 } }),
      frame,
    )
    const scored = toDisplayTrack(
      view({
        threat: {
          risk_score: 60,
          risk_level: 'medium',
          reasons: ['approaching_protected_zone'],
          recommendation: 'ALERT',
        },
      }),
      frame,
    )

    expect(classified.assessment.available).toBe(true)
    expect(scored.assessment.available).toBe(true)
  })

  it('carries reason codes through unchanged', () => {
    const track = toDisplayTrack(
      view({
        threat: {
          risk_score: 81,
          risk_level: 'high',
          reasons: ['approaching_protected_zone', 'persistent_track'],
          recommendation: 'ESCALATE',
        },
      }),
      frame,
    )

    expect(track.assessment.reasons).toEqual([
      'approaching_protected_zone',
      'persistent_track',
    ])
  })
})

describe('site-relative derivation', () => {
  it('derives range and bearing from the site reference', () => {
    // Due north of the site: bearing 0, range roughly one hundredth of a degree of latitude.
    const track = toDisplayTrack(
      view({ position: { latitude: 53.36, longitude: -6.26, altitude_m: 100 } }),
      frame,
    )

    expect(track.siteRelative.bearingDeg).toBeCloseTo(0, 1)
    expect(track.siteRelative.rangeM).toBeGreaterThan(1000)
    expect(track.siteRelative.rangeM).toBeLessThan(1200)
  })

  it('does not alter the altitude the backend reported', () => {
    const track = toDisplayTrack(view(), frame)

    expect(track.siteRelative.altitudeM).toBe(120)
    expect(track.position.altitudeM).toBe(120)
  })
})

describe('history point conversion', () => {
  function historyView(overrides: Partial<TrackHistoryPointView> = {}): TrackHistoryPointView {
    return {
      seq: 7,
      event_type: 'TRACK_UPDATED',
      timestamp: '2026-08-08T12:00:30Z',
      state: 'confirmed',
      position: { latitude: 53.36, longitude: -6.26, altitude_m: 340 },
      speed_mps: 18.5,
      heading_deg: 270,
      ...overrides,
    }
  }

  it('keeps absolute altitude separate from the site-relative ENU height', () => {
    // Regression: the two are different numbers (metres above the ellipsoid vs. metres above
    // this particular site's reference altitude), and conflating them was a real bug caught
    // before this ever reached a browser — the site here is at sea level, so the two happen
    // to coincide, which is exactly the case that would have hidden the bug at a glance.
    const point = toHistoryPoint(historyView(), frame)

    expect(point.altitudeM).toBe(340)
    expect(point.enu.upM).toBeCloseTo(340, 0)
  })

  it('distinguishes absolute altitude from ENU height when the site is not at sea level', () => {
    const raisedSite = new LocalFrame({ latitude: 53.35, longitude: -6.26, altitude_m: 100 })
    const point = toHistoryPoint(historyView({ position: { latitude: 53.36, longitude: -6.26, altitude_m: 340 } }), raisedSite)

    expect(point.altitudeM).toBe(340)
    expect(point.enu.upM).toBeCloseTo(240, 0) // 340m absolute, 100m site reference -> 240m relative
  })

  it('maps backend state through the same vocabulary as a live track', () => {
    expect(toHistoryPoint(historyView({ state: 'stale' }), frame).state).toBe('STALE')
    expect(toHistoryPoint(historyView({ state: 'new' }), frame).state).toBe('DETECTED')
  })

  it('carries the event type and flat kinematics fields through unchanged', () => {
    const point = toHistoryPoint(
      historyView({ event_type: 'TRACK_CONFIRMED', speed_mps: 31.2, heading_deg: 88 }),
      frame,
    )

    expect(point.eventType).toBe('TRACK_CONFIRMED')
    expect(point.speedMps).toBe(31.2)
    expect(point.headingDeg).toBe(88)
    expect(point.seq).toBe(7)
  })

  it('parses the timestamp into a real Date', () => {
    const point = toHistoryPoint(historyView({ timestamp: '2026-08-08T12:00:30Z' }), frame)

    expect(point.timestamp).toBeInstanceOf(Date)
    expect(point.timestamp.toISOString()).toBe('2026-08-08T12:00:30.000Z')
  })
})
