/**
 * Viewport arithmetic: zoom anchoring, pan clamping, and the viewBox mapping.
 *
 * These are the sums a map gets subtly wrong in ways nobody reports as a bug — the view
 * drifts a little on every zoom, the site slides off the edge and cannot be found again, or
 * north ends up pointing down. All of it is pure, so all of it is checked here rather than
 * by squinting at a browser.
 */

import { describe, expect, it } from 'vitest'

import {
  DEFAULT_RANGE_M,
  MAX_RANGE_M,
  MIN_RANGE_M,
  anchoredCentre,
  clampCentre,
  clampRange,
  fullyVisibleRadiusM,
  gridCoverageM,
  viewBoxFor,
} from '../../src/components/map/useMapViewport'

const SITE = { eastM: 0, northM: 0 }

describe('range clamping', () => {
  it('holds the range inside its bounds', () => {
    expect(clampRange(1)).toBe(MIN_RANGE_M)
    expect(clampRange(1e9)).toBe(MAX_RANGE_M)
    expect(clampRange(3_000)).toBe(3_000)
  })

  it('falls back to the default rather than propagating a non-finite range', () => {
    // A viewBox containing NaN renders nothing at all, and an empty map reads as "no tracks"
    // rather than "the display is broken" — the worst possible failure for this component.
    expect(clampRange(Number.NaN)).toBe(DEFAULT_RANGE_M)
    expect(clampRange(Number.POSITIVE_INFINITY)).toBe(MAX_RANGE_M)
  })
})

describe('pan clamping', () => {
  it('leaves a centre within reach untouched', () => {
    const centre = { eastM: 1_000, northM: -500 }
    expect(clampCentre(centre)).toEqual(centre)
  })

  it('stops the view being flung past anywhere a track could be', () => {
    const clamped = clampCentre({ eastM: 5_000_000, northM: 0 })
    expect(Math.hypot(clamped.eastM, clamped.northM)).toBeCloseTo(MAX_RANGE_M, 6)
  })

  it('pulls back along the same bearing rather than per axis', () => {
    // Clamping each axis independently would slide the view sideways along the boundary as
    // an operator dragged into a corner, which feels like the map resisting in one direction.
    const clamped = clampCentre({ eastM: 3_000_000, northM: 3_000_000 })

    expect(clamped.eastM).toBeCloseTo(clamped.northM, 6)
    expect(Math.atan2(clamped.northM, clamped.eastM)).toBeCloseTo(Math.PI / 4, 6)
  })

  it('does not tighten as the range shrinks', () => {
    // Regression. A limit expressed as a multiple of the current range made it impossible to
    // zoom in on a distant track: centring on something 4 km out at a 1.3 km range needs the
    // centre 4 km from the site, and the clamp dragged it back — so the track the operator
    // was zooming towards slid off the edge as they zoomed.
    const distant = { eastM: 4_000, northM: 0 }

    expect(clampCentre(distant)).toEqual(distant)
  })
})

describe('anchored zoom', () => {
  it('keeps the anchored point exactly where it was', () => {
    // The property that matters: after zooming, the anchor must sit at the same fraction of
    // the way from the centre to the edge as it did before. Anything else is drift, and drift
    // compounds — ten notches of a wheel and the thing you aimed at is off-screen.
    const centre = { eastM: 0, northM: 0 }
    const anchor = { eastM: 3_000, northM: 1_000 }
    const from = 5_000
    const to = 2_000

    const moved = anchoredCentre(centre, from, to, anchor)

    const offsetBefore = {
      east: (anchor.eastM - centre.eastM) / from,
      north: (anchor.northM - centre.northM) / from,
    }
    const offsetAfter = {
      east: (anchor.eastM - moved.eastM) / to,
      north: (anchor.northM - moved.northM) / to,
    }

    expect(offsetAfter.east).toBeCloseTo(offsetBefore.east, 9)
    expect(offsetAfter.north).toBeCloseTo(offsetBefore.north, 9)
  })

  it('survives repeated zooming without accumulating drift', () => {
    const anchor = { eastM: 2_500, northM: -1_800 }
    let centre = { eastM: 0, northM: 0 }
    let range = DEFAULT_RANGE_M

    for (let i = 0; i < 12; i++) {
      const next = range / 1.3
      centre = anchoredCentre(centre, range, next, anchor)
      range = next
    }
    for (let i = 0; i < 12; i++) {
      const next = range * 1.3
      centre = anchoredCentre(centre, range, next, anchor)
      range = next
    }

    expect(range).toBeCloseTo(DEFAULT_RANGE_M, 6)
    expect(centre.eastM).toBeCloseTo(0, 6)
    expect(centre.northM).toBeCloseTo(0, 6)
  })

  it('zooms about the centre when there is no anchor', () => {
    const centre = { eastM: 400, northM: 400 }
    expect(anchoredCentre(centre, 5_000, 1_000, null)).toEqual(centre)
  })
})

describe('viewBox mapping', () => {
  function parse(box: string) {
    const [minX, minY, width, height] = box.split(' ').map(Number)
    return { minX: minX!, minY: minY!, width: width!, height: height! }
  }

  it('centres a site-centred viewport on the origin', () => {
    const { minX, minY, width, height } = parse(
      viewBoxFor({ rangeM: 5_000, centre: SITE, isDefault: true }),
    )

    expect(minX).toBeCloseTo(-width / 2, 6)
    expect(minY).toBeCloseTo(-height / 2, 6)
    expect(width).toBe(height)
  })

  it('leaves margin around the outer ring, so the bearing scale is not clipped', () => {
    // The scale is drawn *on* the outer ring. A viewBox of exactly the range would put the
    // north tick and its label hard against the top edge.
    const { width } = parse(viewBoxFor({ rangeM: 5_000, centre: SITE, isDefault: true }))

    expect(width).toBeGreaterThan(10_000)
    expect(width).toBeLessThan(12_000)
  })

  it('inverts the north axis, because the map flips once inside the viewBox', () => {
    // A viewport centred 1000 m north has to look 1000 m *up* the unflipped y axis. Getting
    // this backwards puts the display's north at the bottom, which looks almost right until
    // an operator tries to act on a bearing.
    const centred = parse(viewBoxFor({ rangeM: 1_000, centre: SITE, isDefault: true }))
    const north = parse(
      viewBoxFor({ rangeM: 1_000, centre: { eastM: 0, northM: 1_000 }, isDefault: false }),
    )

    expect(north.minY).toBeCloseTo(centred.minY - 1_000, 6)
    expect(north.minX).toBeCloseTo(centred.minX, 6)
  })

  it('offsets east positively', () => {
    const centred = parse(viewBoxFor({ rangeM: 1_000, centre: SITE, isDefault: true }))
    const east = parse(
      viewBoxFor({ rangeM: 1_000, centre: { eastM: 1_000, northM: 0 }, isDefault: false }),
    )

    expect(east.minX).toBeCloseTo(centred.minX + 1_000, 6)
    expect(east.minY).toBeCloseTo(centred.minY, 6)
  })
})

describe('grid coverage', () => {
  it('reaches past the corner of a site-centred view', () => {
    // Regression. Rings were drawn only as far as the display range, so the corners of the
    // square viewport fell outside the grid — and once panning existed, zooming in on a track
    // a couple of kilometres out left the operator looking at unlit, ungridded black with no
    // distance reference at all.
    const viewport = { rangeM: 5_000, centre: SITE, isDefault: true }
    const { width } = { width: Number(viewBoxFor(viewport).split(' ')[2]) }
    const cornerDistance = (width / 2) * Math.SQRT2

    expect(gridCoverageM(viewport)).toBeGreaterThanOrEqual(cornerDistance)
  })

  it('grows as the view pans away from the site', () => {
    const centred = gridCoverageM({ rangeM: 1_000, centre: SITE, isDefault: false })
    const panned = gridCoverageM({
      rangeM: 1_000,
      centre: { eastM: 4_000, northM: 0 },
      isDefault: false,
    })

    expect(panned).toBeCloseTo(centred + 4_000, 6)
  })
})

describe('bearing scale placement', () => {
  it('sits inside the view when the site is centred', () => {
    const viewport = { rangeM: 5_000, centre: SITE, isDefault: true }
    const half = Number(viewBoxFor(viewport).split(' ')[2]) / 2

    expect(fullyVisibleRadiusM(viewport)).toBeCloseTo(half, 6)
  })

  it('shrinks as the view pans off the site, so the scale stays a complete circle', () => {
    // A partial arc of bearing numerals running off two edges is harder to read than no
    // scale, so the radius tracks the largest circle that actually fits.
    const radius = fullyVisibleRadiusM({
      rangeM: 5_000,
      centre: { eastM: 2_000, northM: 0 },
      isDefault: false,
    })

    expect(radius).toBeCloseTo(5_000 * 1.08 - 2_000, 6)
  })

  it('reports zero once no full circle fits, rather than a negative radius', () => {
    // Drawing a circle with a negative radius is an SVG error; the caller uses the zero to
    // omit the scale entirely.
    expect(
      fullyVisibleRadiusM({
        rangeM: 1_000,
        centre: { eastM: 9_000, northM: 0 },
        isDefault: false,
      }),
    ).toBe(0)
  })
})
