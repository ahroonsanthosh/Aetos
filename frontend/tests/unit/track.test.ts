/**
 * Presentation model: state mapping, and the honesty rules that must not regress.
 */

import { describe, expect, it } from 'vitest'

import type { ThreatView, TrackHistoryPointView, TrackView } from '../../src/data/models/api'
import { LocalFrame } from '../../src/data/models/geo'
import { toDisplayTrack, toHistoryPoint } from '../../src/data/models/track'

const SITE = { latitude: 53.35, longitude: -6.26, altitude_m: 0 }
const frame = new LocalFrame(SITE)

function threatView(overrides: Partial<ThreatView> = {}): ThreatView {
  return {
    risk_score: 0,
    risk_level: 'none',
    confidence: 0,
    reasons: [],
    codes: [],
    factors: [],
    recommendation: 'MONITOR',
    assessed: false,
    ...overrides,
  }
}

function view(overrides: Partial<TrackView> = {}): TrackView {
  return {
    track_id: 'TRK-00001',
    state: 'confirmed',
    displayable: true,
    position: { latitude: 53.36, longitude: -6.25, altitude_m: 120 },
    kinematics: { speed_mps: 20, heading_deg: 90, vertical_rate_mps: 0 },
    classification: { label: 'unknown', confidence: 0, distribution: {}, reasons: [], assessed: false },
    threat: threatView(),
    fusion: {
      assessed: false,
      object_id: null,
      is_primary: true,
      member_track_ids: [],
      corroborating_sensors: [],
    },
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
      view({
        classification: {
          label: 'small_uas',
          confidence: 0.8,
          distribution: { small_uas: 0.8, bird: 0.2 },
          reasons: ['2 sensor reports, all indicating small uas.'],
          assessed: true,
        },
      }),
      frame,
    )
    const scored = toDisplayTrack(
      view({
        threat: threatView({
          risk_score: 60,
          risk_level: 'medium',
          reasons: ['Closing on the site at 24 m/s.'],
          codes: ['closing_velocity'],
          recommendation: 'ALERT',
          assessed: true,
        }),
      }),
      frame,
    )

    expect(classified.assessment.available).toBe(true)
    expect(scored.assessment.available).toBe(true)
  })

  it('separates a classified track from a risk-assessed one', () => {
    // The trap this pins: classification works, risk does not, so a classified track arrives
    // carrying risk NONE at score zero. `available` becomes true — there is something to show
    // — but the risk half must still report itself unassessed, or the panel says "assessed and
    // found harmless" about something nothing has assessed.
    const track = toDisplayTrack(
      view({
        classification: {
          label: 'small_uas',
          confidence: 0.7,
          distribution: { small_uas: 0.7, bird: 0.3 },
          reasons: ['3 sensor reports, all indicating small uas.'],
          assessed: true,
        },
      }),
      frame,
    )

    expect(track.assessment.available).toBe(true)
    expect(track.assessment.classified).toBe(true)
    expect(track.assessment.riskAssessed).toBe(false)
  })

  it('orders the distribution most-probable first and drops zero-probability labels', () => {
    const track = toDisplayTrack(
      view({
        classification: {
          label: 'bird',
          confidence: 0.4,
          distribution: { small_uas: 0.3, bird: 0.5, vehicle: 0, large_uas: 0.2 },
          reasons: [],
          assessed: true,
        },
      }),
      frame,
    )

    expect(track.assessment.distribution.map((entry) => entry.label)).toEqual([
      'bird',
      'small_uas',
      'large_uas',
    ])
  })

  it('shows no distribution for a track classification has not assessed', () => {
    expect(toDisplayTrack(view(), frame).assessment.distribution).toEqual([])
  })

  it('carries reason codes through unchanged', () => {
    const track = toDisplayTrack(
      view({
        threat: threatView({
          risk_score: 81,
          risk_level: 'high',
          reasons: ['approaching_protected_zone', 'persistent_track'],
          recommendation: 'ESCALATE',
          assessed: true,
        }),
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

describe('fusion on a display track', () => {
  const frame = new LocalFrame({ latitude: 53.35, longitude: -6.26, altitude_m: 0 })

  it('carries the object roster through without reshaping it', () => {
    const track = toDisplayTrack(
      view({
        fusion: {
          assessed: true,
          object_id: 'OBJ-0007',
          is_primary: true,
          member_track_ids: ['TRK-0001', 'TRK-0004'],
          corroborating_sensors: ['radar_north', 'rf_central'],
        },
      }),
      frame,
    )

    expect(track.fusion.assessed).toBe(true)
    expect(track.fusion.objectId).toBe('OBJ-0007')
    expect(track.fusion.isPrimary).toBe(true)
    expect(track.fusion.memberTrackIds).toEqual(['TRK-0001', 'TRK-0004'])
    expect(track.fusion.corroboratingSensors).toEqual(['radar_north', 'rf_central'])
  })

  it('keeps "not assessed" distinct from "assessed and standing alone"', () => {
    // The two render differently and must therefore stay distinguishable in the model. Nothing
    // may collapse `assessed: false` into an empty roster — "nobody looked" is not a finding.
    const unassessed = toDisplayTrack(view(), frame)
    const alone = toDisplayTrack(
      view({
        fusion: {
          assessed: true,
          object_id: 'OBJ-0001',
          is_primary: true,
          member_track_ids: ['TRK-0001'],
          corroborating_sensors: ['radar_north'],
        },
      }),
      frame,
    )

    expect(unassessed.fusion.assessed).toBe(false)
    expect(alone.fusion.assessed).toBe(true)
    expect(alone.fusion.memberTrackIds).toHaveLength(1)
  })
})

describe('what the live picture presents', () => {
  /** The rule `useTrackPicture` applies, mirroring `GET /tracks`. Extracted so the predicate
   *  itself is under test rather than the hook's plumbing. */
  const presented = (v: TrackView) => v.fusion.is_primary || !v.fusion.assessed

  const fused = (id: string, isPrimary: boolean, objectId: string) =>
    view({
      track_id: id,
      fusion: {
        assessed: true,
        object_id: objectId,
        is_primary: isPrimary,
        member_track_ids: isPrimary ? [id] : [],
        corroborating_sensors: [],
      },
    })

  it('shows one track per object, not one per observation', () => {
    // Three sensors watching one drone produce three tracks; fusion elects one primary. The
    // map must draw one marker. This is FM-023, and the live picture regressed into it because
    // the event stream carries supporting tracks even though the REST bootstrap does not.
    const all = [
      fused('TRK-00001', true, 'OBJ-0001'),
      fused('TRK-00002', false, 'OBJ-0001'),
      fused('TRK-00003', false, 'OBJ-0001'),
    ]

    const shown = all.filter(presented)

    expect(shown).toHaveLength(1)
    expect(shown[0]?.track_id).toBe('TRK-00001')
  })

  it('keeps every track when no fusion service is attached', () => {
    // The dangerous direction. `assessed: false` means nobody looked, and filtering on
    // `is_primary` alone would empty the operator's display entirely rather than showing an
    // unreconciled picture (ADR 0011).
    const all = [view({ track_id: 'TRK-00001' }), view({ track_id: 'TRK-00002' })]

    expect(all.every((v) => v.fusion.assessed)).toBe(false)
    expect(all.filter(presented)).toHaveLength(2)
  })

  it('still presents a supporting track that fusion later released', () => {
    const released = view({
      track_id: 'TRK-00009',
      fusion: {
        assessed: true,
        object_id: 'OBJ-0004',
        is_primary: true,
        member_track_ids: ['TRK-00009'],
        corroborating_sensors: [],
      },
    })

    expect(presented(released)).toBe(true)
  })
})
