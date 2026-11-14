/**
 * The parts of the Aetos backend API the twin reads, and how it reads them.
 *
 * The twin is served by the backend at `/twin/`, so these calls are same-origin and carry the
 * console's session cookie: an operator signed in to the console is signed in here too.
 */

export const API_PREFIX = '/api/v1';

export interface GeoPoint {
  latitude: number;
  longitude: number;
}

export interface Position extends GeoPoint {
  altitude_m: number;
}

export interface ZoneView {
  zone_id: string;
  name: string;
  kind: 'monitored' | 'restricted' | 'protected' | string;
  geometry:
    | { shape: 'circle'; centre: GeoPoint; radius_m: number }
    | { shape: 'polygon'; vertices: GeoPoint[] };
  floor_m: number | null;
  ceiling_m: number | null;
}

export interface SiteView {
  site_id: string;
  name: string;
  reference: Position;
  protected_radius_m: number;
  zones: ZoneView[];
}

export interface SensorView {
  sensor_id: string;
  site_id: string;
  registry_key: string | null;
  hardware_driver: string | null;
  sensor_type: string | null;
  position: GeoPoint;
  height_m: number;
  boresight_deg: number;
  enabled: boolean;
  coverage?: {
    min_range_m: number;
    max_range_m: number;
    azimuth_extent_deg: number;
    elevation_min_deg: number;
    elevation_max_deg: number;
  } | null;
}

export interface FactorView {
  code: string;
  contribution: number;
  detail: string;
}

export interface TrackView {
  track_id: string;
  state: 'new' | 'tentative' | 'confirmed' | 'stale' | 'lost';
  displayable: boolean;
  position: Position;
  kinematics: { speed_mps: number; heading_deg: number; vertical_rate_mps: number };
  classification: { label: string; confidence: number; assessed: boolean };
  threat: {
    risk_score: number;
    risk_level: 'none' | 'low' | 'medium' | 'high' | 'critical';
    reasons: string[];
    factors: FactorView[];
    recommendation: 'MONITOR' | 'VERIFY' | 'ALERT' | 'ESCALATE' | 'RESPONSE_RECOMMENDED';
    assessed: boolean;
  };
  tracking: { contributing_sensors: string[] };
}

/** A request that failed, with the status the twin needs to explain why. */
export class LiveFeedError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'LiveFeedError';
  }
}

export type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export async function getJson<T>(fetchImpl: Fetch, path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_PREFIX}${path}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal,
    });
  } catch (cause) {
    if (signal?.aborted) throw cause;
    throw new LiveFeedError(0, 'The Aetos backend is not reachable.');
  }
  if (!response.ok) {
    throw new LiveFeedError(
      response.status,
      response.status === 401
        ? 'Sign in to the Aetos console to see live tracks here.'
        : `The Aetos backend answered ${response.status} for ${path}.`,
    );
  }
  return (await response.json()) as T;
}
