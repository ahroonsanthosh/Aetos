/**
 * Latitude/longitude to local ENU, and back.
 *
 * Every real-world input this editor accepts — an OSM building, a geocoded address, a GPS
 * point clicked on a map — arrives in lat/lon. Everything Aetos actually renders and reasons
 * about is in the site's local ENU frame, metres from its datum (ADR 0004). This file is the
 * one conversion between them, so a sign error here cannot hide inside a dozen call sites.
 *
 * Equirectangular approximation: longitude is scaled by cos(datum latitude) and both axes
 * are treated as locally flat. That is wrong at the scale of a country and indistinguishable
 * from correct at the scale of a site — the error is under a centimetre per kilometre at
 * mid-latitudes, and every site this application draws fits inside a few kilometres.
 */

import type { Ground } from '@/types';

/** Mean Earth radius, metres — good enough at site scale; the WGS-84 ellipsoid's ~0.3%
 *  flattening is smaller than the approximation error already accepted above. */
const EARTH_RADIUS_M = 6_371_000;

const DEG_TO_RAD = Math.PI / 180;
const RAD_TO_DEG = 180 / Math.PI;

export interface LatLon {
  latitude: number;
  longitude: number;
}

/** Metres per degree of latitude. Constant enough to treat as such at site scale. */
export function metresPerDegreeLatitude(): number {
  return (EARTH_RADIUS_M * Math.PI) / 180;
}

/** Metres per degree of longitude at a given latitude — shrinks toward the poles. */
export function metresPerDegreeLongitude(atLatitude: number): number {
  return metresPerDegreeLatitude() * Math.cos(atLatitude * DEG_TO_RAD);
}

/** A lat/lon point's position in the site's local ENU frame, metres from the datum. */
export function latLonToEnu(point: LatLon, datum: LatLon): Ground {
  return {
    e: (point.longitude - datum.longitude) * metresPerDegreeLongitude(datum.latitude),
    n: (point.latitude - datum.latitude) * metresPerDegreeLatitude(),
  };
}

/** Inverse of `latLonToEnu` — a ground point back to lat/lon, for round-tripping a map pick. */
export function enuToLatLon(point: Ground, datum: LatLon): LatLon {
  return {
    latitude: datum.latitude + point.n / metresPerDegreeLatitude(),
    longitude: datum.longitude + point.e / metresPerDegreeLongitude(datum.latitude),
  };
}

/**
 * Bounding box, in degrees, covering a circle of `radiusMetres` around `centre`.
 *
 * Slightly generous rather than exact — a query bbox is a filter, not a shape, so erring
 * wide costs a few extra results from Overpass rather than a missed building at the edge.
 */
export function boundingBoxAround(
  centre: LatLon,
  radiusMetres: number,
): { south: number; west: number; north: number; east: number } {
  const dLat = radiusMetres / metresPerDegreeLatitude();
  const dLon = radiusMetres / metresPerDegreeLongitude(centre.latitude);
  return {
    south: centre.latitude - dLat,
    north: centre.latitude + dLat,
    west: centre.longitude - dLon,
    east: centre.longitude + dLon,
  };
}

/** Great-circle distance, metres. Used to size an auto-suggested site boundary. */
export function haversineMetres(a: LatLon, b: LatLon): number {
  const dLat = (b.latitude - a.latitude) * DEG_TO_RAD;
  const dLon = (b.longitude - a.longitude) * DEG_TO_RAD;
  const lat1 = a.latitude * DEG_TO_RAD;
  const lat2 = b.latitude * DEG_TO_RAD;

  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

export { RAD_TO_DEG, DEG_TO_RAD };
