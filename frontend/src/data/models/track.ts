/**
 * Presentation model: the wire `TrackView` reshaped for display.
 *
 * One explicit conversion point, so a backend schema change surfaces here rather than in
 * thirty components.
 *
 * Two decisions are recorded in this file rather than scattered through the UI.
 *
 * **Track state vocabulary.** The frontend specification lists seven display states
 * (DETECTED, ACQUIRING, TRACKING, MONITORED, LOST, STALE, DISCARDED). The backend has five
 * (new, tentative, confirmed, stale, lost). The five that exist are mapped onto the
 * specification's preferred words; MONITORED and DISCARDED are **not** implemented, because
 * a state the system cannot enter has no business appearing in an interface that an operator
 * is meant to read as a description of reality.
 *
 * **Assessment availability.** Classification and risk are not implemented in the backend
 * (ADR 0011). Rather than let components each decide what an unassessed track looks like,
 * the conversion marks assessment as unavailable and every consumer renders that one way.
 */

import type {
  BackendTrackState,
  ClassificationLabel,
  EventType,
  Recommendation,
  RiskLevel,
  TrackHistoryPointView,
  TrackView,
} from './api'
import type { EnuPoint, LocalFrame, SiteRelative } from './geo'

/** Display vocabulary. Exactly one per backend state — no invented states. */
export type TrackDisplayState =
  | 'DETECTED'
  | 'ACQUIRING'
  | 'TRACKING'
  | 'STALE'
  | 'LOST'

const STATE_LABELS: Record<BackendTrackState, TrackDisplayState> = {
  new: 'DETECTED',
  tentative: 'ACQUIRING',
  confirmed: 'TRACKING',
  stale: 'STALE',
  lost: 'LOST',
}

const STATE_DESCRIPTIONS: Record<TrackDisplayState, string> = {
  DETECTED: 'A single observation. Not yet evidence of an object.',
  ACQUIRING: 'Accumulating support. May still turn out to be clutter.',
  TRACKING: 'Enough consistent detections to believe an object is really there.',
  STALE: 'Detections have stopped arriving. Position is predicted, not observed.',
  LOST: 'No longer maintained. Retained in history.',
}

export interface TrackAssessment {
  /**
   * Whether the backend has actually assessed this track.
   *
   * False throughout this build: the classification and risk modules are not implemented, so
   * every track arrives with the default label and a zero score. Rendering that default as
   * though it were a finding would present an unassessed track as assessed-and-benign, which
   * is the more dangerous of the two mistakes.
   */
  readonly available: boolean
  readonly label: ClassificationLabel
  readonly labelConfidence: number
  readonly riskScore: number
  readonly riskLevel: RiskLevel
  readonly reasons: readonly string[]
  readonly recommendation: Recommendation
}

export interface DisplayTrack {
  readonly id: string
  readonly state: TrackDisplayState
  readonly stateDescription: string
  readonly displayable: boolean

  readonly position: { latitude: number; longitude: number; altitudeM: number }
  /** Derived from the site reference for display. Site-relative, not sensor-relative. */
  readonly siteRelative: SiteRelative
  readonly enu: { eastM: number; northM: number; upM: number }

  readonly speedMps: number
  readonly headingDeg: number
  readonly verticalRateMps: number

  readonly assessment: TrackAssessment

  readonly firstSeen: Date
  readonly lastSeen: Date
  readonly updatedAt: Date
  readonly detectionCount: number
  readonly missedUpdates: number
  readonly coasting: boolean
  readonly trackConfidence: number
  readonly contributingSensors: readonly string[]
  readonly lastAppliedSeq: number
}

export function toDisplayTrack(view: TrackView, frame: LocalFrame): DisplayTrack {
  const state = STATE_LABELS[view.state]
  const enu = frame.toEnu(view.position)

  return {
    id: view.track_id,
    state,
    stateDescription: STATE_DESCRIPTIONS[state],
    displayable: view.displayable,

    position: {
      latitude: view.position.latitude,
      longitude: view.position.longitude,
      altitudeM: view.position.altitude_m,
    },
    siteRelative: frame.toSiteRelative(view.position),
    enu,

    speedMps: view.kinematics.speed_mps,
    headingDeg: view.kinematics.heading_deg,
    verticalRateMps: view.kinematics.vertical_rate_mps,

    assessment: toAssessment(view),

    firstSeen: new Date(view.tracking.first_seen),
    lastSeen: new Date(view.tracking.last_seen),
    updatedAt: new Date(view.updated_at),
    detectionCount: view.tracking.detection_count,
    missedUpdates: view.tracking.missed_updates,
    coasting: view.tracking.coasting,
    trackConfidence: view.tracking.track_confidence,
    contributingSensors: view.tracking.contributing_sensors,
    lastAppliedSeq: view.last_applied_seq,
  }
}

/**
 * Decide whether the backend has said anything about this track beyond where it is.
 *
 * The test is deliberately structural rather than a build-time flag: an unknown label at zero
 * confidence with no reason codes is precisely what an unassessed track looks like, and it is
 * also what the assessment modules will stop producing the moment they exist. So this starts
 * reporting `available: true` on its own, without a code change, as soon as there is
 * something real to report.
 */
function toAssessment(view: TrackView): TrackAssessment {
  const { classification, threat } = view

  const classified = classification.label !== 'unknown' || classification.confidence > 0
  const assessed = threat.risk_score > 0 || threat.reasons.length > 0

  return {
    available: classified || assessed,
    label: classification.label,
    labelConfidence: classification.confidence,
    riskScore: threat.risk_score,
    riskLevel: threat.risk_level,
    reasons: threat.reasons,
    recommendation: threat.recommendation,
  }
}

/** Track colour by state. Returns a token name, never a literal colour. */
export function trackColourToken(track: DisplayTrack, selected: boolean): string {
  if (selected) return 'var(--track-selected)'
  if (track.state === 'LOST') return 'var(--track-lost)'
  if (track.state === 'STALE') return 'var(--track-stale)'
  if (track.assessment.available) {
    switch (track.assessment.riskLevel) {
      case 'critical':
      case 'high':
        return 'var(--status-critical)'
      case 'medium':
        return 'var(--status-warning)'
      default:
        break
    }
  }
  return 'var(--track-default)'
}

/**
 * One point in a track's recorded history — the route it has actually taken.
 *
 * Unlike `DisplayTrack`, which is one live snapshot, this is a single entry from
 * `GET /tracks/{id}/history`: one point per event the backend recorded for the track, not a
 * fixed-interval sample. A track observed every second for a minute has roughly sixty of
 * these; a track that sat quietly CONFIRMED between two coasting cycles has few. The spacing
 * between points is itself real information — it says how often something actually happened,
 * not how often the display refreshed.
 */
export interface TrackHistoryPoint {
  readonly seq: number
  readonly eventType: EventType
  readonly timestamp: Date
  readonly state: TrackDisplayState
  /** Site-relative position, for plotting the trail on the map. */
  readonly enu: EnuPoint
  /** Absolute altitude, metres above the WGS-84 ellipsoid — matches how altitude is shown
   *  everywhere else in the interface. Kept alongside `enu` rather than derived from
   *  `enu.upM`, which is height above the *site's* reference altitude, a different number. */
  readonly altitudeM: number
  readonly speedMps: number
  readonly headingDeg: number
}

export function toHistoryPoint(view: TrackHistoryPointView, frame: LocalFrame): TrackHistoryPoint {
  return {
    seq: view.seq,
    eventType: view.event_type,
    timestamp: new Date(view.timestamp),
    state: STATE_LABELS[view.state],
    enu: frame.toEnu(view.position),
    altitudeM: view.position.altitude_m,
    speedMps: view.speed_mps,
    headingDeg: view.heading_deg,
  }
}
