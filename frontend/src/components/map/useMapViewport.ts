/**
 * What part of the world the map is looking at: how far out, and centred where.
 *
 * Kept separate from the map component, and built from pure functions, because viewport
 * arithmetic is the part of a map most likely to be subtly wrong in a way no one notices —
 * a zoom that drifts, a pan that accelerates, a clamp that lets the site slide off the edge
 * and never come back. All of that is testable without a DOM, so it is.
 *
 * The display is a plan position indicator: the *site* is the origin of the coordinate
 * system, always, and panning moves the viewport over that fixed frame rather than moving
 * the world. `centre` is therefore expressed in the same ENU metres as everything else, and
 * `{0, 0}` means "looking straight at the site".
 */

import { useCallback, useMemo, useState } from 'react'

export interface EnuPoint2D {
  readonly eastM: number
  readonly northM: number
}

export interface MapViewport {
  /** Half-extent of the visible square, in metres. The outer range ring's radius. */
  readonly rangeM: number
  /** Where the viewport is centred, in site-relative ENU metres. */
  readonly centre: EnuPoint2D
  /** True when looking straight at the site at the default range — i.e. nothing to reset. */
  readonly isDefault: boolean
}

export const DEFAULT_RANGE_M = 5_000
export const MIN_RANGE_M = 200
export const MAX_RANGE_M = 50_000

/** Multiplier per zoom step. 1.3 gives roughly four steps per decade — coarse enough that
 *  the buttons feel like they do something, fine enough that a wheel does not overshoot. */
const ZOOM_FACTOR = 1.3

/**
 * How far the centre may stray from the site, in metres.
 *
 * Bounded so a careless drag cannot fling the view a thousand kilometres into empty space,
 * but bounded *absolutely* rather than as a multiple of the current range.
 *
 * The multiple-of-range version was tried first and was wrong in a way that only showed up
 * against real data: zooming in on a track 4 km out at a 1.3 km range needs the centre 4 km
 * from the site, which "two screens" forbids — so the clamp quietly dragged the view back and
 * the track the operator was zooming towards slid off the edge. A clamp that fights the
 * gesture it is supposed to permit is worse than no clamp.
 *
 * The furthest the display can ever show is `MAX_RANGE_M`, so anywhere within that of the site
 * is somewhere a track could plausibly be, and therefore somewhere an operator may legitimately
 * want to look. Beyond it there is nothing to see. Recentring is always one click away
 * regardless, which is what actually guarantees nobody gets lost.
 */
const PAN_LIMIT_M = MAX_RANGE_M

export function useMapViewport(): {
  viewport: MapViewport
  setRange: (rangeM: number) => void
  /** Zoom by `steps` notches (positive zooms in), keeping `anchor` under the same pixel. */
  zoomAt: (steps: number, anchor: EnuPoint2D | null) => void
  panBy: (deltaEastM: number, deltaNorthM: number) => void
  reset: () => void
} {
  // Range and centre are one piece of state, not two.
  //
  // They were two, with the zoom handler calling `setCentre` from inside the `setRangeM`
  // updater. React updaters must be pure and may be invoked more than once for a single
  // update, so that nesting made the centre update run a different number of times than the
  // range update — and the anchored point drifted a couple of percent per gesture, the same
  // amount at any scroll speed, which is what ruled out event batching and pointed here.
  //
  // A zoom is one transition of one viewport. Modelling it as one state makes the updater
  // pure and the drift arithmetically impossible rather than merely unobserved.
  const [state, setState] = useState<ViewportState>({
    rangeM: DEFAULT_RANGE_M,
    centre: { eastM: 0, northM: 0 },
  })

  const setRange = useCallback((next: number) => {
    setState((current) => ({ rangeM: clampRange(next), centre: clampCentre(current.centre) }))
  }, [])

  const zoomAt = useCallback((steps: number, anchor: EnuPoint2D | null) => {
    setState((current) => {
      const rangeM = clampRange(current.rangeM / ZOOM_FACTOR ** steps)
      return {
        rangeM,
        centre: clampCentre(anchoredCentre(current.centre, current.rangeM, rangeM, anchor)),
      }
    })
  }, [])

  const panBy = useCallback((deltaEastM: number, deltaNorthM: number) => {
    setState((current) => ({
      rangeM: current.rangeM,
      centre: clampCentre({
        eastM: current.centre.eastM + deltaEastM,
        northM: current.centre.northM + deltaNorthM,
      }),
    }))
  }, [])

  const reset = useCallback(() => {
    setState({ rangeM: DEFAULT_RANGE_M, centre: { eastM: 0, northM: 0 } })
  }, [])

  const viewport = useMemo<MapViewport>(
    () => ({
      rangeM: state.rangeM,
      centre: state.centre,
      isDefault:
        state.rangeM === DEFAULT_RANGE_M &&
        state.centre.eastM === 0 &&
        state.centre.northM === 0,
    }),
    [state],
  )

  return { viewport, setRange, zoomAt, panBy, reset }
}

interface ViewportState {
  rangeM: number
  centre: EnuPoint2D
}

// --------------------------------------------------------------------------------------
// Pure viewport arithmetic — exported for direct testing
// --------------------------------------------------------------------------------------

export function clampRange(rangeM: number): number {
  // NaN only. An infinity clamps perfectly well through Math.min and should behave like any
  // other out-of-range number; NaN cannot, because every comparison against it is false, so
  // it would slip through the clamp and put a NaN in the viewBox — which renders an empty
  // map that reads as "no tracks" rather than "the display is broken".
  if (Number.isNaN(rangeM)) return DEFAULT_RANGE_M
  return Math.min(MAX_RANGE_M, Math.max(MIN_RANGE_M, rangeM))
}

export function clampCentre(centre: EnuPoint2D): EnuPoint2D {
  const distance = Math.hypot(centre.eastM, centre.northM)
  if (distance <= PAN_LIMIT_M) return centre

  // Pulled back along the same bearing rather than clamped per-axis, which would slide the
  // view sideways along the boundary as an operator dragged into the corner.
  const scale = PAN_LIMIT_M / distance
  return { eastM: centre.eastM * scale, northM: centre.northM * scale }
}

/**
 * The centre that keeps `anchor` under the same pixel while the range changes.
 *
 * Zooming about the cursor rather than about the middle is the difference between a map that
 * feels like a map and one that fights you: without it, the thing you are pointing at is the
 * thing that runs away as you zoom towards it.
 */
export function anchoredCentre(
  centre: EnuPoint2D,
  fromRangeM: number,
  toRangeM: number,
  anchor: EnuPoint2D | null,
): EnuPoint2D {
  if (!anchor || fromRangeM <= 0) return centre

  const scale = toRangeM / fromRangeM
  return {
    eastM: anchor.eastM + (centre.eastM - anchor.eastM) * scale,
    northM: anchor.northM + (centre.northM - anchor.northM) * scale,
  }
}

/**
 * Margin around the outer range ring, as a fraction of the range.
 *
 * `rangeM` is the outer ring's radius, and the bearing scale is drawn on that ring — so a
 * viewBox of exactly ±rangeM puts the north tick and its label hard against the top edge,
 * where they clip. The margin is here rather than in the components so `rangeM` keeps one
 * meaning everywhere: the distance the outer ring represents, not "the outer ring, less
 * whatever each component decided to reserve for itself".
 */
export const VIEW_MARGIN = 1.08

/**
 * The SVG viewBox for a viewport.
 *
 * The y term is negated because the viewBox is expressed in pre-flip coordinates — the map's
 * single `scale(1, -1)` happens inside it — so a viewport centred on 1000 m *north* has to
 * look 1000 m *up* the unflipped axis.
 */
export function viewBoxFor({ rangeM, centre }: MapViewport): string {
  const half = rangeM * VIEW_MARGIN
  const minX = centre.eastM - half
  const minY = -centre.northM - half
  return `${minX} ${minY} ${half * 2} ${half * 2}`
}

/**
 * How far from the site the grid has to reach to fill the screen.
 *
 * The range rings and the scope face are centred on the *site*, not on the viewport, because
 * that is what they mean — distance from the site is the quantity being displayed. Drawing
 * them only as far as `rangeM` was fine while the view was always site-centred and became
 * obviously wrong the moment panning existed: zooming in on a track 2.5 km out left the
 * operator looking at unlit, ungridded black with no distance reference at all.
 *
 * The far corner of a square viewport is `half · √2` from its centre, and the centre is
 * `|centre|` from the site, so that sum is the furthest point on screen from the site.
 */
export function gridCoverageM({ rangeM, centre }: MapViewport): number {
  const half = rangeM * VIEW_MARGIN
  return Math.hypot(centre.eastM, centre.northM) + half * Math.SQRT2
}

/**
 * The largest radius around the site that is entirely on screen, or zero if none is.
 *
 * The bearing scale is drawn on a *complete* circle — a partial arc of bearing numerals
 * running off two edges is harder to read than no scale at all. When the operator has panned
 * far enough that no full ring fits, there is nowhere honest to put the scale, and it is
 * simply not drawn; the rings still give distance.
 */
export function fullyVisibleRadiusM({ rangeM, centre }: MapViewport): number {
  const half = rangeM * VIEW_MARGIN
  return Math.max(0, half - Math.hypot(centre.eastM, centre.northM))
}
