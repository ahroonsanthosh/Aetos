/**
 * Aetos Command — domain contract.
 *
 * This file is the boundary between "where the data came from" and "how it is drawn".
 * The simulation layer produces these types; the state layer stores them; the 3D scene and
 * the UI consume them. Nothing downstream may ask whether a value was simulated.
 *
 * When Aetos Core replaces the simulator, the transport changes and this file does not.
 */

// ---------------------------------------------------------------------------
// Spatial frame
// ---------------------------------------------------------------------------

/**
 * Local East-North-Up frame in metres, origin at the site datum.
 *
 * This mirrors the tracking frame the real Aetos backend uses (ADR 0004) rather than
 * three.js world coordinates, so that Core positions can be consumed unmodified. The
 * single conversion to renderer axes lives in `three/frame.ts` and nowhere else.
 */
export interface Enu {
  /** Metres east of the site datum. */
  e: number;
  /** Metres north of the site datum. */
  n: number;
  /** Metres above the site datum plane. */
  u: number;
}

/** A ground-plane position. Used for footprints, roads and zone outlines. */
export interface Ground {
  e: number;
  n: number;
}

// ---------------------------------------------------------------------------
// Site and assets
// ---------------------------------------------------------------------------

export type AssetType =
  | 'substation'
  | 'transformer'
  | 'inverter'
  | 'control_building'
  | 'solar_array'
  | 'storage'
  | 'gate';

/** Criticality is operator-declared, 1–10. It is an input to threat, never an output of it. */
export type Criticality = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10;

export type AssetStatus = 'operational' | 'degraded' | 'offline' | 'maintenance';

export interface Asset {
  id: string;
  name: string;
  type: AssetType;
  /** Centroid of the physical asset. */
  position: Enu;
  /** Planar extent used for selection bounds and camera framing, metres. */
  footprint: { width: number; depth: number; height: number };
  /** Rotation about the vertical axis, radians. */
  heading: number;
  criticality: Criticality;
  /** Id of the protected volume guarding this asset, if any. */
  protectionZoneId?: string;
  status: AssetStatus;
  /** Ids of sensors whose coverage includes this asset. */
  sensorsCovering: string[];
  /** Track ids currently assessed as threatening this asset. */
  activeThreats: string[];
  /**
   * 0–1. How well Aetos believes it can currently protect this asset, given sensor
   * availability, coverage, health and response resources. Simulated in this prototype.
   */
  protectionScore: number;
}

// ---------------------------------------------------------------------------
// Protected zones
// ---------------------------------------------------------------------------

export type ZoneClass = 'perimeter' | 'restricted' | 'critical';

/**
 * Zones are volumes, not circles on a map. A zone is a closed ground polygon extruded
 * between a floor and a ceiling altitude.
 */
export interface Zone {
  id: string;
  name: string;
  zoneClass: ZoneClass;
  /** Closed polygon, counter-clockwise, in ground coordinates. */
  polygon: Ground[];
  /** Metres above the datum plane. */
  floor: number;
  ceiling: number;
  /** Asset this zone exists to protect, if it guards a specific one. */
  protectsAssetId?: string;
  /**
   * Current emphasis, 0–1, derived from the threat picture. Drives how visible the volume
   * is. At 0 the zone is almost invisible; at 1 it is unmistakable.
   */
  emphasis: number;
  /** Track ids currently inside the volume. */
  intruderTrackIds: string[];
  /** Track ids whose predicted trajectory enters the volume. */
  predictedIntruderTrackIds: string[];
}

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------

export type SensorType = 'radar' | 'camera' | 'thermal' | 'rf' | 'acoustic';

export type SensorStatus = 'online' | 'degraded' | 'offline';

/**
 * Sensor coverage as a swept volume: a sector of an annulus, extruded in elevation.
 * Deliberately crude — it is a prototype's stand-in for a real propagation model, and the
 * shape of the type is what matters for the eventual swap.
 */
export interface SensorCoverage {
  /** Metres. Minimum range is non-zero for radar, which has a blind cone. */
  minRange: number;
  maxRange: number;
  /** Bearing of the coverage centreline, radians clockwise from north. */
  bearing: number;
  /** Total horizontal width of the sector, radians. A full 2π means omnidirectional. */
  azimuthWidth: number;
  /** Elevation limits, radians above the horizontal. */
  minElevation: number;
  maxElevation: number;
}

export interface Sensor {
  id: string;
  name: string;
  type: SensorType;
  position: Enu;
  /** Height of the sensor head above its base, metres. */
  mastHeight: number;
  status: SensorStatus;
  /** 0–1 device health, independent of whether conditions currently degrade it. */
  health: number;
  /**
   * 0–1 effectiveness right now, after weather and status are applied. This is what the
   * coverage model consumes. `health` is the device; `effectiveness` is the situation.
   */
  effectiveness: number;
  coverage: SensorCoverage;
  /** Human-readable cause when effectiveness is below health. */
  degradationReason?: string;
}

// ---------------------------------------------------------------------------
// Coverage map
// ---------------------------------------------------------------------------

export type CoverageQuality = 'strong' | 'reduced' | 'gap';

/** One cell of the site coverage grid. The grid answers "where am I vulnerable?". */
export interface CoverageCell {
  e: number;
  n: number;
  /** 0–1 aggregate detection confidence for this cell. */
  value: number;
  quality: CoverageQuality;
}

export interface CoverageGrid {
  /** Cell size, metres. */
  resolution: number;
  /** Half-extent of the grid about the datum, metres. */
  extent: number;
  cells: CoverageCell[];
  /** Compass sector with the weakest aggregate coverage, for the readiness narrative. */
  weakestSector: string;
}

// ---------------------------------------------------------------------------
// Tracks
// ---------------------------------------------------------------------------

export type Classification =
  | 'uav'
  | 'probable_uav'
  | 'aircraft'
  | 'helicopter'
  | 'bird'
  | 'balloon'
  | 'unknown';

/** What the object is doing — separate from what it is, and from whose it is. */
export type Behaviour =
  | 'transiting'
  | 'approaching'
  | 'loitering'
  | 'surveying'
  | 'departing'
  | 'probing';

/** Whose it is. Absence of authorisation is not evidence of hostility. */
export type Identity = 'authorised' | 'known' | 'unknown';

export type TrackStatus = 'tentative' | 'confirmed' | 'coasting' | 'lost';

export type ThreatLevel = 'low' | 'medium' | 'high' | 'critical';

/** A past or future point on a track's path. */
export interface TrackPoint {
  position: Enu;
  /** Milliseconds since epoch. */
  t: number;
}

/**
 * The named contributions to a threat score. Carried alongside the number so the interface
 * can always answer "why is this high?" without recomputing anything.
 */
export interface ThreatFactor {
  code: string;
  label: string;
  /** Points this factor contributed to the total. */
  contribution: number;
}

export interface ThreatAssessment {
  /** 0–100. Continuous and expected to move. */
  score: number;
  level: ThreatLevel;
  factors: ThreatFactor[];
  /** Asset the object is currently assessed as threatening, if any. */
  threatenedAssetId?: string;
  /** Seconds until the predicted path enters a protected volume. Undefined if it does not. */
  etaToProtectedZone?: number;
  /** Zone the predicted path is expected to enter. */
  predictedZoneId?: string;
}

export interface Track {
  trackId: string;
  classification: Classification;
  /** 0–1 confidence in the classification, not in the existence of the object. */
  confidence: number;
  position: Enu;
  /** Metres above the datum plane. Duplicated from position.u for read clarity. */
  altitude: number;
  /** Metres per second, ENU components. */
  velocity: Enu;
  /** Metres per second, magnitude. */
  speed: number;
  /** Radians clockwise from north. */
  heading: number;
  /** Metres from the site datum. */
  range: number;
  behaviour: Behaviour;
  identity: Identity;
  status: TrackStatus;
  threat: ThreatAssessment;
  /** Observed path, oldest first. Bounded by the simulator. */
  history: TrackPoint[];
  /** Extrapolated path, nearest first. Distinct from history everywhere it is drawn. */
  predicted: TrackPoint[];
  /** Ids of the sensors contributing observations to this track. */
  contributingSensors: string[];
}

// ---------------------------------------------------------------------------
// Response resources
// ---------------------------------------------------------------------------

export type ResourceStatus = 'available' | 'committed' | 'unavailable' | 'maintenance';

/**
 * Inventory only. This prototype models what exists and what state it is in. It does not
 * task, command, or control anything, and must not acquire the ability to.
 */
export interface ResponseResource {
  id: string;
  name: string;
  kind: string;
  position: Enu;
  status: ResourceStatus;
  /** 0–1. */
  readiness: number;
  batteryPercent?: number;
  communications: 'connected' | 'intermittent' | 'lost';
  currentTask?: string;
}

/**
 * The only response output this prototype produces: advice, addressed to a human.
 *
 * There is deliberately no `authorise` or `execute` anywhere in this codebase. Those are
 * separate states with separate authors — see the repository scope boundary.
 */
export type RecommendationCategory =
  | 'monitor'
  | 'verify'
  | 'alert'
  | 'escalate'
  | 'operator_review_required';

export interface Recommendation {
  trackId: string;
  category: RecommendationCategory;
  /** Plain-language justification shown to the operator. */
  rationale: string;
}

// ---------------------------------------------------------------------------
// System health
// ---------------------------------------------------------------------------

export interface SubsystemHealth {
  id: string;
  label: string;
  /** 0–1. */
  value: number;
  status: 'nominal' | 'degraded' | 'offline';
}

export interface SystemHealth {
  /** 0–1 overall readiness. */
  readiness: number;
  subsystems: SubsystemHealth[];
  /** Single most important weakness, in plain language. A number alone is not useful. */
  primaryWeakness?: string;
  /** What to do about it. */
  recommendedAction?: string;
}

// ---------------------------------------------------------------------------
// Environment
// ---------------------------------------------------------------------------

export type WeatherCondition = 'clear' | 'rain' | 'fog' | 'wind' | 'night';

export interface Weather {
  condition: WeatherCondition;
  label: string;
  /** Per-sensor-type multipliers applied to effectiveness, 0–1. */
  sensorImpact: Partial<Record<SensorType, number>>;
}

// ---------------------------------------------------------------------------
// Site
// ---------------------------------------------------------------------------

export interface Site {
  id: string;
  name: string;
  kind: string;
  /** Geodetic datum the ENU frame is anchored to. Display only in this prototype. */
  datum: { latitude: number; longitude: number };
  /** Site boundary polygon, ground coordinates. */
  boundary: Ground[];
  assets: Asset[];
  sensors: Sensor[];
  zones: Zone[];
  resources: ResponseResource[];
  /** Static scenery: road centrelines. */
  roads: Ground[][];
  /** Static scenery: solar array block definitions. */
  arrays: SolarArrayBlock[];
}

/**
 * A rectangular block of solar tables. Expanded into instanced geometry at render time —
 * never into individual objects in the data model.
 */
export interface SolarArrayBlock {
  id: string;
  assetId: string;
  origin: Ground;
  /** Rotation of the whole block about the vertical axis, radians. */
  heading: number;
  rows: number;
  tablesPerRow: number;
  /** Metres between row centrelines. */
  rowPitch: number;
  /** Metres between table centres along a row. */
  tablePitch: number;
}

// ---------------------------------------------------------------------------
// Snapshot — the unit of transport
// ---------------------------------------------------------------------------

/**
 * One complete picture of the situation at one instant.
 *
 * The simulator emits these at a fixed rate. A WebSocket from Aetos Core would emit the
 * same shape. The renderer interpolates between the last two, which is what makes the
 * transport swap invisible: nothing in the scene assumes a frame-rate data source.
 */
export interface Snapshot {
  /** Milliseconds since epoch, from the source's clock, not the browser's. */
  t: number;
  /** Monotonic sequence number. Gaps mean dropped snapshots. */
  seq: number;
  tracks: Track[];
  /** Zone state at this instant, keyed by zone id. */
  zones: Record<string, Pick<Zone, 'emphasis' | 'intruderTrackIds' | 'predictedIntruderTrackIds'>>;
  /** Asset state at this instant, keyed by asset id. */
  assets: Record<string, Pick<Asset, 'status' | 'activeThreats' | 'protectionScore'>>;
  /** Sensor state at this instant, keyed by sensor id. */
  sensors: Record<string, Pick<Sensor, 'status' | 'health' | 'effectiveness' | 'degradationReason'>>;
  coverage: CoverageGrid;
  health: SystemHealth;
  weather: Weather;
  recommendations: Recommendation[];
}

// ---------------------------------------------------------------------------
// Interface modes
// ---------------------------------------------------------------------------

/**
 * What the operator is currently trying to understand. Modes change emphasis across the
 * whole environment, not just which panel is open.
 */
export type ViewMode = 'overview' | 'tactical' | 'threat' | 'system';

export type SelectionKind = 'track' | 'asset' | 'sensor';

export interface Selection {
  kind: SelectionKind;
  id: string;
}
