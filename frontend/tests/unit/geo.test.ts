/**
 * The local tangent frame, and the round trip between metres and degrees.
 *
 * `toGeodetic` exists for the basemap layer, which has to be told where to look in latitude
 * and longitude while the rest of the display thinks entirely in metres from the site. A frame
 * whose inverse is only approximately its forward transform would put the basemap slightly out
 * of register with the tracks drawn on it — a small, plausible-looking error that an operator
 * cannot detect from the display.
 */

import { describe, expect, it } from 'vitest'

import { LocalFrame, bearingFromEnu, compassPoint } from '../../src/data/models/geo'

const SITE = { latitude: 53.35, longitude: -6.26, altitude_m: 40 }

describe('round trip', () => {
  it('returns the input exactly, not approximately', () => {
    const frame = new LocalFrame(SITE)
    const enu = { eastM: 4_137.5, northM: -2_904.25 }

    const geodetic = frame.toGeodetic(enu)
    const back = frame.toEnu({ ...geodetic, altitude_m: SITE.altitude_m })

    expect(back.eastM).toBeCloseTo(enu.eastM, 9)
    expect(back.northM).toBeCloseTo(enu.northM, 9)
  })

  it('holds at high latitude, where a degree of longitude is short', () => {
    // The term most likely to be wrong is the cos(latitude) scaling on longitude, and it is
    // hardest to notice near the equator where it is close to 1.
    const frame = new LocalFrame({ latitude: 71.0, longitude: 25.78, altitude_m: 0 })
    const enu = { eastM: 12_000, northM: 8_000 }

    const back = frame.toEnu({ ...frame.toGeodetic(enu), altitude_m: 0 })

    expect(back.eastM).toBeCloseTo(enu.eastM, 9)
    expect(back.northM).toBeCloseTo(enu.northM, 9)
  })

  it('maps the origin to the site itself', () => {
    const frame = new LocalFrame(SITE)
    const centre = frame.toGeodetic({ eastM: 0, northM: 0 })

    expect(centre.latitude).toBeCloseTo(SITE.latitude, 12)
    expect(centre.longitude).toBeCloseTo(SITE.longitude, 12)
  })
})

describe('direction', () => {
  it('sends east and north to increasing longitude and latitude', () => {
    // A sign error mirrors the world and stays plausible-looking until something is compared
    // against a landmark.
    const frame = new LocalFrame(SITE)

    expect(frame.toGeodetic({ eastM: 1_000, northM: 0 }).longitude).toBeGreaterThan(
      SITE.longitude,
    )
    expect(frame.toGeodetic({ eastM: 0, northM: 1_000 }).latitude).toBeGreaterThan(SITE.latitude)
  })

  it('shortens a degree of longitude with latitude', () => {
    // 1000 m east is a larger angular step the further north you are.
    const dublin = new LocalFrame(SITE)
    const equator = new LocalFrame({ latitude: 0, longitude: 0, altitude_m: 0 })

    const dublinStep = dublin.toGeodetic({ eastM: 1_000, northM: 0 }).longitude - SITE.longitude
    const equatorStep = equator.toGeodetic({ eastM: 1_000, northM: 0 }).longitude

    expect(dublinStep).toBeGreaterThan(equatorStep)
  })

  it('reports bearings clockwise from north', () => {
    expect(bearingFromEnu({ eastM: 0, northM: 100, upM: 0 })).toBeCloseTo(0, 6)
    expect(bearingFromEnu({ eastM: 100, northM: 0, upM: 0 })).toBeCloseTo(90, 6)
    expect(bearingFromEnu({ eastM: 0, northM: -100, upM: 0 })).toBeCloseTo(180, 6)
    expect(bearingFromEnu({ eastM: -100, northM: 0, upM: 0 })).toBeCloseTo(270, 6)
  })

  it('names the compass point nearest a bearing', () => {
    expect(compassPoint(0)).toBe('N')
    expect(compassPoint(46)).toBe('NE')
    expect(compassPoint(359)).toBe('N')
  })
})
