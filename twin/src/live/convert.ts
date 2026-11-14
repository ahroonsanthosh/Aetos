/**
 * Aetos backend views, turned into the twin's own types.
 *
 * Pure functions: no fetching, no clock, no state. `LiveSource` owns all of that, which keeps
 * everything here testable against plain objects.
 *
 * What the twin shows for a live track is the backend's own assessment: its risk score, level,
 * reasons and recommendation. The twin does not re-score anything. The only figures derived
 * here are geometric (position in the local frame, a straight-line prediction, and when that
 * line enters a zone) because they are what the 3D scene draws.
 */

import type {
  Behaviour,
  Classification,
  Enu,
  Recommendation,
  RecommendationCategory,
  Sensor,
  SensorType,
  Site,
  ThreatLevel,
  Track,
  TrackPoint,
  TrackStatus,
  Zone,
  ZoneClass,
} from '@/types';
import { firstEntryIntoVolume, horizontalDistance, pointInVolume } from '@/lib/geometry';
import { latLonToEnu, type LatLon } from '@/site/import/geo';
import { extrapolate } from '@/simulation/flight';
import type { SensorView, SiteView, TrackView, ZoneView } from './api';

/** A zone with no ceiling is unbounded upward; a volume needs a top to be drawn. */
export const UNBOUNDED_CEILING_M = 500;
const CIRCLE_SEGMENTS = 48;
const DEG = Math.PI / 180;

// ---------------------------------------------------------------------------
// Site
// ---------------------------------------------------------------------------

export function buildSite(view: SiteView, sensors: SensorView[]): Site {
  const datum: LatLon = { latitude: view.reference.latitude, longitude: view.reference.longitude };
  const refAlt = view.reference.altitude_m;

  const zones = view.zones.map((z) => toZone(z, datum, refAlt));
  const twinSensors = sensors
    .filter((s) => s.enabled)
    .map((s) => toSensor(s, datum));

  // The scene is framed on the protected area, at twice its radius: close enough that the site
  // and the tracks approaching it are legible. Wider zones still render; they just run past
  // the frame, as a 2.5 km restricted ring or an 8 km monitored area would otherwise put the
  // camera kilometres back and shrink the site to nothing.
  const radius = view.protected_radius_m * 2;

  return {
    id: view.site_id,
    name: view.name,
    kind: 'Aetos site · live',
    datum,
    boundary: circle({ e: 0, n: 0 }, radius),
    assets: [],
    sensors: twinSensors,
    zones,
    resources: [],
    roads: [],
    arrays: [],
  };
}

const ZONE_CLASS: Record<string, ZoneClass> = {
  monitored: 'perimeter',
  restricted: 'restricted',
  protected: 'critical',
};

export function toZone(view: ZoneView, datum: LatLon, refAlt: number): Zone {
  const polygon =
    view.geometry.shape === 'circle'
      ? circle(latLonToEnu(view.geometry.centre, datum), view.geometry.radius_m)
      : view.geometry.vertices.map((v) => latLonToEnu(v, datum));

  return {
    id: view.zone_id,
    name: view.name,
    zoneClass: ZONE_CLASS[view.kind] ?? 'perimeter',
    polygon: counterClockwise(polygon),
    floor: view.floor_m === null ? 0 : Math.max(0, view.floor_m - refAlt),
    ceiling: view.ceiling_m === null ? UNBOUNDED_CEILING_M : view.ceiling_m - refAlt,
    emphasis: 0,
    intruderTrackIds: [],
    predictedIntruderTrackIds: [],
  };
}

const SENSOR_TYPE: Record<string, SensorType> = {
  radar: 'radar',
  simulated_radar: 'radar',
  eo_ir: 'camera',
  simulated_eo_ir: 'camera',
  rf: 'rf',
  simulated_rf: 'rf',
  acoustic: 'acoustic',
  simulated_acoustic: 'acoustic',
};

/** Used only when the backend reports no coverage (a fitted, unmeasured sensor). */
const DEFAULT_RANGE_M: Record<SensorType, number> = {
  radar: 3000,
  camera: 1500,
  thermal: 1500,
  rf: 3000,
  acoustic: 600,
};

export function toSensor(view: SensorView, datum: LatLon): Sensor {
  const type = SENSOR_TYPE[view.sensor_type ?? ''] ?? 'radar';
  const ground = latLonToEnu(view.position, datum);
  const c = view.coverage;
  const name = view.sensor_id.replace(/_/g, ' ');

  return {
    id: view.sensor_id,
    name: name.charAt(0).toUpperCase() + name.slice(1),
    type,
    position: { e: ground.e, n: ground.n, u: view.height_m },
    mastHeight: view.height_m,
    status: 'online',
    health: 1,
    effectiveness: 1,
    coverage: {
      minRange: c?.min_range_m ?? 0,
      maxRange: c?.max_range_m ?? DEFAULT_RANGE_M[type],
      bearing: view.boresight_deg * DEG,
      azimuthWidth: Math.min(360, c?.azimuth_extent_deg ?? 360) * DEG,
      minElevation: (c?.elevation_min_deg ?? -5) * DEG,
      maxElevation: (c?.elevation_max_deg ?? 60) * DEG,
    },
  };
}

// ---------------------------------------------------------------------------
// Tracks
// ---------------------------------------------------------------------------

const STATUS: Record<TrackView['state'], TrackStatus | null> = {
  new: 'tentative',
  tentative: 'tentative',
  confirmed: 'confirmed',
  stale: 'coasting',
  lost: null,
};

const LEVEL: Record<TrackView['threat']['risk_level'], ThreatLevel> = {
  none: 'low',
  low: 'low',
  medium: 'medium',
  high: 'high',
  critical: 'critical',
};

const ADVICE: Record<TrackView['threat']['recommendation'], RecommendationCategory> = {
  MONITOR: 'monitor',
  VERIFY: 'verify',
  ALERT: 'alert',
  ESCALATE: 'escalate',
  RESPONSE_RECOMMENDED: 'operator_review_required',
};

export function classify(label: string, confidence: number): Classification {
  if (label === 'bird') return 'bird';
  if (label.endsWith('_uas')) return confidence >= 0.6 && label !== 'unknown_uas' ? 'uav' : 'probable_uav';
  return 'unknown';
}

export function behaviourOf(position: Enu, velocity: Enu): Behaviour {
  const speed = Math.hypot(velocity.e, velocity.n);
  if (speed < 2.5) return 'loitering';
  const range = Math.max(horizontalDistance(position, { e: 0, n: 0, u: 0 }), 1e-3);
  const closing = -(velocity.e * position.e + velocity.n * position.n) / range;
  if (closing > speed * 0.45) return 'approaching';
  if (closing < -speed * 0.45) return 'departing';
  return 'transiting';
}

/**
 * One backend track as a twin track. Returns null for tracks the console does not display
 * (lost, or not yet displayable), so the twin never shows more than the console does.
 */
export function toTrack(
  view: TrackView,
  site: Site,
  refAlt: number,
  history: TrackPoint[],
  nowMs: number,
): Track | null {
  const status = STATUS[view.state];
  if (!status || !view.displayable) return null;

  const ground = latLonToEnu(view.position, site.datum);
  const position: Enu = { e: ground.e, n: ground.n, u: view.position.altitude_m - refAlt };
  const heading = view.kinematics.heading_deg * DEG;
  const speed = view.kinematics.speed_mps;
  const velocity: Enu = {
    e: speed * Math.sin(heading),
    n: speed * Math.cos(heading),
    u: view.kinematics.vertical_rate_mps,
  };
  const predicted = extrapolate(position, velocity, nowMs);

  // When the straight-line prediction first enters a protected zone, for the scene's emphasis
  // and the panel's ETA. Geometry only; the risk score beside it is the backend's. Only
  // protected zones count, and only ones the track is not already inside: being within a wide
  // monitored area is not "about to enter a protected volume".
  let predictedZoneId: string | undefined;
  let eta: number | undefined;
  for (const zone of site.zones) {
    if (zone.zoneClass !== 'critical') continue;
    if (pointInVolume(position, zone.polygon, zone.floor, zone.ceiling)) continue;
    const entry = firstEntryIntoVolume(predicted, zone.polygon, zone.floor, zone.ceiling);
    if (entry && (eta === undefined || (entry.t - nowMs) / 1000 < eta)) {
      eta = (entry.t - nowMs) / 1000;
      predictedZoneId = zone.id;
    }
  }

  const assessed = view.threat.assessed;
  return {
    trackId: view.track_id,
    classification: view.classification.assessed
      ? classify(view.classification.label, view.classification.confidence)
      : 'unknown',
    confidence: view.classification.confidence,
    position,
    altitude: position.u,
    velocity,
    speed,
    heading,
    range: horizontalDistance(position, { e: 0, n: 0, u: 0 }),
    behaviour: behaviourOf(position, velocity),
    identity: 'unknown',
    status,
    threat: {
      score: assessed ? view.threat.risk_score : 0,
      level: assessed ? LEVEL[view.threat.risk_level] : 'low',
      factors: view.threat.factors.map((f) => ({
        code: f.code,
        label: f.detail,
        contribution: Math.round(f.contribution * 100),
      })),
      etaToProtectedZone: eta,
      predictedZoneId,
    },
    history,
    predicted,
    contributingSensors: view.tracking.contributing_sensors,
  };
}

/** The backend's recommendation for a track, in the twin's vocabulary. */
export function adviceFor(view: TrackView): Recommendation {
  return {
    trackId: view.track_id,
    category: ADVICE[view.threat.recommendation] ?? 'monitor',
    rationale: view.threat.assessed
      ? (view.threat.reasons[0] ?? 'Assessed by Aetos.')
      : 'Not yet assessed by Aetos.',
  };
}

// ---------------------------------------------------------------------------
// Geometry helpers
// ---------------------------------------------------------------------------

function circle(centre: { e: number; n: number }, radius: number) {
  return Array.from({ length: CIRCLE_SEGMENTS }, (_, i) => {
    const a = (i / CIRCLE_SEGMENTS) * Math.PI * 2;
    return { e: centre.e + radius * Math.cos(a), n: centre.n + radius * Math.sin(a) };
  });
}

/** Zones are counter-clockwise in the twin's types; site files may arrive either way. */
function counterClockwise(polygon: { e: number; n: number }[]) {
  let area = 0;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    area += (polygon[j]!.e - polygon[i]!.e) * (polygon[j]!.n + polygon[i]!.n);
  }
  return area > 0 ? polygon : [...polygon].reverse();
}
