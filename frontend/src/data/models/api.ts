/**
 * Wire types, mirroring the backend's API schemas exactly.
 *
 * These are a transcription of `backend/api/schemas.py` and `backend/api/system.py`, not an
 * interpretation. Field names, optionality and units match the backend byte for byte, so a
 * mismatch shows up as a type error rather than as an undefined at runtime.
 *
 * Nothing here is reshaped for the interface's convenience. Presentation types live in
 * `src/data/models/track.ts`, and the conversion between the two is one explicit place —
 * which is what keeps a backend change from leaking into every component.
 *
 * Units, from docs/detection_schema.md: metres, metres per second, degrees clockwise from
 * true north, WGS-84, altitude above the WGS-84 ellipsoid, timestamps UTC ISO-8601.
 */

export const API_PREFIX = '/api/v1'

/** Backend lifecycle states. These five are the only states a track can be in. */
export type BackendTrackState = 'new' | 'tentative' | 'confirmed' | 'stale' | 'lost'

export type ClassificationLabel =
  | 'unknown'
  | 'unknown_uas'
  | 'small_uas'
  | 'medium_uas'
  | 'large_uas'
  | 'bird'
  | 'vehicle'
  | 'other'

export type RiskLevel = 'none' | 'low' | 'medium' | 'high' | 'critical'

export type Recommendation =
  | 'MONITOR'
  | 'VERIFY'
  | 'ALERT'
  | 'ESCALATE'
  | 'RESPONSE_RECOMMENDED'

export interface PositionView {
  latitude: number
  longitude: number
  altitude_m: number
}

export interface KinematicsView {
  speed_mps: number
  heading_deg: number
  vertical_rate_mps: number
}

export interface ClassificationView {
  label: ClassificationLabel
  /** How much this conclusion should be believed — deliberately *not* the top label's
   *  probability. Folds distribution peakedness, evidence weight and track maturity, so a
   *  sharp answer built on one detection reads as uncertain (ADR 0012). */
  confidence: number
  /** Probability across the label set. Empty means nothing has been assessed, which is
   *  different from having been assessed as `unknown`. */
  distribution: Partial<Record<ClassificationLabel, number>>
  /** One human-readable justification per contributing evidence source. */
  reasons: string[]
  /** Computed server-side so every client does not re-derive the same emptiness check and
   *  eventually derive it differently. */
  assessed: boolean
}

export interface ThreatView {
  risk_score: number
  risk_level: RiskLevel
  /** Always present alongside the score. A bare number is a black box an operator cannot act on. */
  reasons: string[]
  recommendation: Recommendation
}

export interface TrackingView {
  first_seen: string
  /** When the last *detection* arrived — not the last update. A coasting track is updated by
   *  prediction without being seen, and conflating the two makes a coasted position look
   *  freshly observed. */
  last_seen: string
  detection_count: number
  missed_updates: number
  coasting: boolean
  track_confidence: number
  contributing_sensors: string[]
}

export interface TrackView {
  track_id: string
  state: BackendTrackState
  /** Whether the backend considers this track fit to show on the operator's map. Computed
   *  server-side deliberately: a client deciding for itself would eventually decide
   *  differently, and a single unconfirmed return is what clutter looks like. */
  displayable: boolean
  position: PositionView
  kinematics: KinematicsView
  classification: ClassificationView
  threat: ThreatView
  tracking: TrackingView
  updated_at: string
  /** The log position this view was projected from. Lets the client hand off from REST to the
   *  event stream at exactly the right sequence number, with no gap and no duplicate. */
  last_applied_seq: number
}

export interface TrackListResponse {
  tracks: TrackView[]
  count: number
  /** The log position this whole snapshot is current to. A client that opens the event stream
   *  at `as_of_seq + 1` sees neither a gap nor a duplicate. Note this is the snapshot's
   *  position; each TrackView carries its own `last_applied_seq` for that individual track. */
  as_of_seq: number
}

export interface TrackHistoryPointView {
  seq: number
  /** Which event produced this point — TRACK_CREATED, TRACK_UPDATED, TRACK_CONFIRMED,
   *  TRACK_STALE, TRACK_LOST. One point per recorded state change, not a fixed-interval
   *  sample, so the spacing between points reflects how often the track actually changed. */
  event_type: EventType
  timestamp: string
  state: BackendTrackState
  position: PositionView
  /** Flat, not nested under a kinematics object — matches the wire shape exactly. */
  speed_mps: number
  heading_deg: number
}

export interface TrackHistoryResponse {
  track_id: string
  /** Oldest first. */
  points: TrackHistoryPointView[]
  count: number
}

export interface AlertView {
  alert_id: string
  seq: number
  created_at: string
  track_id: string | null
  source: string
  cleared: boolean
  detail: Record<string, unknown>
}

export interface AlertListResponse {
  alerts: AlertView[]
  count: number
}

export interface GeoPointView {
  latitude: number
  longitude: number
}

export interface SensorView {
  sensor_id: string
  site_id: string
  registry_key: string
  /** Null when the backend has not established the sensor's type. The registry entry that
   *  would state it has no loader behind the repository layer, and the backend leaves the
   *  field null rather than inventing one — so the interface must render its absence. */
  sensor_type: string | null
  position: GeoPointView
  height_m: number
  boresight_deg: number
  enabled: boolean
}

export interface SensorListResponse {
  sensors: SensorView[]
  count: number
}

export type ZoneKind = 'monitored' | 'restricted' | 'protected'

export interface CircleGeometryView {
  shape: 'circle'
  centre: GeoPointView
  radius_m: number
}

export interface PolygonGeometryView {
  shape: 'polygon'
  vertices: GeoPointView[]
}

export type ZoneGeometryView = CircleGeometryView | PolygonGeometryView

export interface ZoneView {
  zone_id: string
  name: string
  kind: ZoneKind
  geometry: ZoneGeometryView
  floor_m: number | null
  ceiling_m: number | null
}

export interface SiteView {
  site_id: string
  name: string
  reference: PositionView
  protected_radius_m: number
  zones: ZoneView[]
}

export interface SiteListResponse {
  sites: SiteView[]
  count: number
}

export interface ScenarioView {
  scenario_id: string
  name: string
  description: string
  seed: number
  duration_s: number
}

export interface ScenarioListResponse {
  scenarios: ScenarioView[]
  count: number
}

export interface DetectionView {
  seq: number
  detection_id: string
  timestamp: string
  sensor_id: string
  sensor_type: string
  position: PositionView
  position_uncertainty_m: number
  confidence: number
}

/* -------------------------------------------------------------------------------------- */
/* System status                                                                            */
/* -------------------------------------------------------------------------------------- */

export type RunMode = 'simulation' | 'live' | 'hybrid'

export interface ModeView {
  mode: RunMode
  real_adapters_connected: number
  /** Computed from whether a real adapter is actually feeding the pipeline, not from the
   *  configured mode string. The two can disagree, and the operator needs to see that. */
  simulated: boolean
}

/**
 * `not_built` is deliberately distinct from both healthy and broken. A module that does not
 * exist is not an outage, and showing it green would be a lie.
 */
export type ComponentState =
  | 'ok'
  | 'degraded'
  | 'unavailable'
  | 'not_built'
  | 'not_connected'

export interface ComponentHealth {
  component: string
  state: ComponentState
  detail: string
}

export interface SystemHealthView {
  components: ComponentHealth[]
  latest_event_seq: number
  projection_seq: number
  /** Events committed but not yet projected. Growing without bound means the operator's
   *  picture is falling behind what the system actually knows. */
  projection_lag: number
}

/* -------------------------------------------------------------------------------------- */
/* Event stream                                                                             */
/* -------------------------------------------------------------------------------------- */

export type EventType =
  | 'DETECTION_RECEIVED'
  | 'DETECTION_REJECTED'
  | 'DETECTION_SHED'
  | 'TRACK_CREATED'
  | 'TRACK_UPDATED'
  | 'TRACK_CONFIRMED'
  | 'TRACK_COASTING'
  | 'TRACK_STALE'
  | 'TRACK_LOST'
  | 'CLASSIFICATION_CHANGED'
  | 'RISK_CHANGED'
  | 'ZONE_ENTERED'
  | 'ZONE_EXITED'
  | 'ALERT_CREATED'
  | 'ALERT_CLEARED'
  | 'RECOMMENDATION_CHANGED'
  | 'OPERATOR_ACKNOWLEDGED'
  | 'SCENARIO_STARTED'
  | 'SCENARIO_STOPPED'
  | 'SENSOR_STATUS_CHANGED'
  | 'DEGRADED_MODE_ENTERED'
  | 'DEGRADED_MODE_EXITED'
  | 'SYSTEM_STARTED'
  | 'SUBSCRIBER_FAILED'

export interface StreamedEvent {
  /** Position in the total order. The ordering authority — not `timestamp`, which comes from
   *  sensors whose clocks may disagree. */
  seq: number
  event_id: string
  event_type: EventType
  timestamp: string
  source: string
  track_id: string | null
  /** Full track state, present on every event that changes a track. This is what lets the
   *  client maintain the picture from the stream alone. */
  track: TrackView | null
  detail: Record<string, unknown>
}
