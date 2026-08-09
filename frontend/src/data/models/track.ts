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
 * **Assessment availability.** Classification and risk each report their own `assessed` flag
 * from the backend. Rather than let components each decide what an unassessed track looks
 * like, the conversion resolves them into one `available` flag plus the two specific ones, and
 * every consumer renders those the same way (ADR 0011).
 */

import type {
  BackendTrackState,
  ClassificationLabel,
  EventType,
  FactorView,
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

/** One risk factor as displayed: what was observed, and what it did to the score. */
export interface RiskFactor {
  readonly code: string
  readonly weight: number
  readonly activation: number
  readonly contribution: number
  readonly detail: string
  /** Whether the factor found anything. A checked-and-silent factor is worth showing as such;
   *  it is how an operator sees a concern was considered rather than overlooked. */
  readonly material: boolean
}

export interface TrackAssessment {
  /**
   * Whether the backend has concluded anything at all about this track — either half.
   *
   * A track can be classified without being risk-scored, so this is the weaker "there is
   * something to show" test, and `classified` / `riskAssessed` below say which half. Rendering
   * a track's *defaults* as though they were findings would present an unassessed track as
   * assessed-and-benign, which is the more dangerous of the two mistakes.
   */
  readonly available: boolean
  readonly label: ClassificationLabel
  readonly labelConfidence: number
  /** Probability across the label set, ordered most-probable first for display. Empty when
   *  classification has not assessed this track. */
  readonly distribution: ReadonlyArray<{ label: ClassificationLabel; probability: number }>
  /** Why classification concluded what it did — one entry per contributing evidence source. */
  readonly classificationReasons: readonly string[]
  /** Whether classification specifically has assessed this track, as reported by the backend
   *  rather than inferred from the shape of the data. */
  readonly classified: boolean
  /** Whether risk assessment has actually run, as reported by the backend. Distinguishing it
   *  from a genuine zero matters: "we looked and nothing here is concerning" and "nothing has
   *  looked" must never render the same way. */
  readonly riskAssessed: boolean
  /** How much the risk assessment should be believed. Never folded into the score. */
  readonly riskConfidence: number
  /** Machine-readable reason codes, ordered as `reasons`. */
  readonly riskCodes: readonly string[]
  /** Every factor consulted, including the silent ones, ordered by contribution. */
  readonly riskFactors: readonly RiskFactor[]
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
 * Both halves now report their own `assessed` flag and both are taken from the backend rather
 * than inferred here. The backend is the authority on whether it assessed something, and a
 * client re-deriving it would eventually derive it differently — which was a live risk while
 * risk assessment was inferred structurally from "score is zero and there are no reasons".
 * That heuristic was about to become wrong in the most dangerous direction: the engine can
 * legitimately return a score of zero, meaning "we looked and nothing here is concerning",
 * and the structural test would have reported that genuine finding as *not assessed*.
 */
function toAssessment(view: TrackView): TrackAssessment {
  const { classification, threat } = view

  const distribution = Object.entries(classification.distribution ?? {})
    .map(([label, probability]) => ({
      label: label as ClassificationLabel,
      probability: probability ?? 0,
    }))
    .filter((entry) => entry.probability > 0)
    .sort((a, b) => b.probability - a.probability)

  return {
    available: classification.assessed || threat.assessed,
    label: classification.label,
    labelConfidence: classification.confidence,
    distribution,
    classificationReasons: classification.reasons ?? [],
    classified: classification.assessed,
    riskAssessed: threat.assessed,
    riskConfidence: threat.confidence,
    riskScore: threat.risk_score,
    riskLevel: threat.risk_level,
    reasons: threat.reasons,
    riskCodes: threat.codes ?? [],
    riskFactors: (threat.factors ?? []).map(toRiskFactor),
    recommendation: threat.recommendation,
  }
}

function toRiskFactor(view: FactorView): RiskFactor {
  return {
    code: view.code,
    weight: view.weight,
    activation: view.activation,
    contribution: view.contribution,
    detail: view.detail,
    material: view.activation > 0 && view.weight > 0,
  }
}

/** Track colour by state. Returns a token name, never a literal colour. */
export function trackColourToken(track: DisplayTrack, selected: boolean): string {
  if (selected) return 'var(--track-selected)'
  if (track.state === 'LOST') return 'var(--track-lost)'
  if (track.state === 'STALE') return 'var(--track-stale)'
  // Keyed on `riskAssessed`, not `available`: a classified track carries a default risk level
  // of `none`, and colouring from it would paint an unassessed track in a "checked, benign"
  // colour. Every track therefore stays neutral until risk actually runs, which is correct.
  if (track.assessment.riskAssessed) {
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
