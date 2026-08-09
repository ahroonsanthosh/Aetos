/**
 * Geodetic to site-local conversion, for display only.
 *
 * The backend does this exactly, in a WGS-84 ENU tangent plane (ADR 0004), and it is the
 * authority. This module exists because the API hands the interface WGS-84 coordinates while
 * the PPI display is site-centred and needs metres, range and bearing (ADR 0010).
 *
 * Two things follow from "display only":
 *
 * 1. This is an approximation — a local tangent plane using the earth's radii of curvature at
 *    the site latitude. At counter-UAS ranges the error against the backend's exact ECEF
 *    conversion is well under a metre, far below sensor uncertainty and far below one screen
 *    pixel. It is never used for a decision, only to place a marker.
 * 2. Range and bearing computed here are **site-relative**, measured from the protected site's
 *    reference point. They are not what any sensor observed. The interface labels them as
 *    such, because a bearing an operator reads as "what the radar saw" would be a
 *    misrepresentation of a value nothing measured.
 */

import type { PositionView } from './api'

/** WGS-84 defining parameters. */
const SEMI_MAJOR_AXIS_M = 6_378_137.0
const FLATTENING = 1.0 / 298.257_223_563
const ECCENTRICITY_SQ = FLATTENING * (2.0 - FLATTENING)

export interface EnuPoint {
  /** Metres east of the site reference. */
  eastM: number
  /** Metres north of the site reference. */
  northM: number
  /** Metres above the site reference altitude. */
  upM: number
}

export interface SiteRelative {
  /** Horizontal distance from the site reference, metres. */
  rangeM: number
  /** Degrees clockwise from true north, as seen from the site reference. */
  bearingDeg: number
  /** Metres above the WGS-84 ellipsoid, straight from the backend. Not derived. */
  altitudeM: number
}

/**
 * A local tangent plane anchored at one geodetic origin.
 *
 * Built once per site and reused, so the radii of curvature are computed once rather than per
 * track per frame.
 */
export class LocalFrame {
  private readonly metresPerDegreeLatitude: number
  private readonly metresPerDegreeLongitude: number

  constructor(private readonly origin: PositionView) {
    const latRad = (origin.latitude * Math.PI) / 180
    const sinLat = Math.sin(latRad)
    const denominator = 1 - ECCENTRICITY_SQ * sinLat * sinLat

    // Meridional radius of curvature — how far a degree of latitude is at this latitude.
    const meridional =
      (SEMI_MAJOR_AXIS_M * (1 - ECCENTRICITY_SQ)) / Math.pow(denominator, 1.5)

    // Radius of curvature in the prime vertical, scaled by cos(latitude): this is the term
    // that makes a degree of longitude shrink towards the poles, and getting it wrong is
    // exactly the distortion ADR 0004 exists to avoid.
    const primeVertical = SEMI_MAJOR_AXIS_M / Math.sqrt(denominator)

    this.metresPerDegreeLatitude = (meridional * Math.PI) / 180
    this.metresPerDegreeLongitude = (primeVertical * Math.cos(latRad) * Math.PI) / 180
  }

  toEnu(position: PositionView): EnuPoint {
    return {
      eastM: (position.longitude - this.origin.longitude) * this.metresPerDegreeLongitude,
      northM: (position.latitude - this.origin.latitude) * this.metresPerDegreeLatitude,
      upM: position.altitude_m - this.origin.altitude_m,
    }
  }

  /**
   * ENU metres back to geodetic degrees — the inverse of `toEnu`.
   *
   * Added for the basemap layer, which has to be told where to centre in latitude and
   * longitude while the rest of the display thinks entirely in metres from the site. Exact
   * inverse of `toEnu` by construction: both use the same two per-degree scale factors, so a
   * round trip returns the input rather than something close to it.
   */
  toGeodetic(enu: { eastM: number; northM: number }): { latitude: number; longitude: number } {
    return {
      latitude: this.origin.latitude + enu.northM / this.metresPerDegreeLatitude,
      longitude: this.origin.longitude + enu.eastM / this.metresPerDegreeLongitude,
    }
  }

  /** The site's own reference position. */
  get reference(): PositionView {
    return this.origin
  }

  /** Site-relative range, bearing and altitude, for the track details panel. */
  toSiteRelative(position: PositionView): SiteRelative {
    const enu = this.toEnu(position)
    return {
      rangeM: Math.hypot(enu.eastM, enu.northM),
      bearingDeg: bearingFromEnu(enu),
      altitudeM: position.altitude_m,
    }
  }
}

/** Compass bearing of an ENU offset, degrees clockwise from north. */
export function bearingFromEnu(enu: EnuPoint): number {
  const degrees = (Math.atan2(enu.eastM, enu.northM) * 180) / Math.PI
  return (degrees + 360) % 360
}

/** Eight-point compass abbreviation, for labelling a bearing readably. */
export function compassPoint(bearingDeg: number): string {
  const points = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
  const index = Math.round(((bearingDeg % 360) / 45)) % 8
  return points[index] ?? 'N'
}
