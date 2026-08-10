# `components/map`

## Responsibility

The operational picture: a site-centred PPI in SVG. Draws range rings, zones, sensor
placements and track markers in the site's local ENU frame, in metres.

Selection drives information density. An unselected track shows a lean marker with an
optional single-line data block (`MapDisplayOptions` / `useMapDisplayOptions`, toggled by the
operator and persisted to `localStorage`) and a short predicted path. The selected track gets
a history trail (`TrackTrail`, backed by `useTrackHistory`), an extended predicted path with
time-tick marks (`PredictedPath`), and a full data block regardless of the toggle state.
Deselecting reverts all three immediately — there is no separate "declutter" step, it falls
out of `selected` going back to `false` and the trail fetch simply stopping.

## Navigation

Scroll to zoom about the cursor, drag to pan, double-click to recentre on the site. Range
presets remain because a counter-UAS operator's questions are scale-specific — "what is inside
1 km" is one click, and getting back to exactly 5 km by wheel is fiddly.

Viewport arithmetic lives in `useMapViewport` as pure functions, tested without a DOM. That is
deliberate: the sums a map gets wrong are the ones nobody reports as a bug, because the symptom
is a view that drifts slightly per zoom or a site that slides off and cannot be found again.

Two consequences worth knowing when changing this:

- **Range and centre are one piece of state.** A zoom is one transition of one viewport, so it
  must be one pure updater. Splitting them and calling `setCentre` from inside the `setRangeM`
  updater put a side effect in a function React may invoke more than once.
- **The grid covers the view, not the range.** Rings and the scope face are centred on the
  *site* — distance from the site is what they measure — but are drawn out to `gridCoverageM`,
  the furthest on-screen point from it. Drawing them only to `rangeM` was fine while the view
  was always site-centred and broke the moment panning existed: zooming towards a track a few
  kilometres out left the operator in unlit, ungridded black.

The bearing scale (`CompassRose`) sits on the largest circle around the site that is *entirely*
on screen, and is omitted once none is. A partial arc of bearing numerals running off two edges
is harder to read than no scale at all.

| File | Responsibility |
|---|---|
| `OperationalMap.tsx` | Layout, the SVG viewport, pointer navigation, wires selection to history |
| `BasemapLayer.tsx` / `useBasemap.ts` | The optional geographic layer and its toggle |
| `basemapCamera.ts` | ENU viewport → Web Mercator camera, so tiles register with the display |
| `useMapViewport.ts` | Range and centre: zoom anchoring, pan clamping, viewBox and grid extents |
| `MapControls.tsx` | Zoom buttons, range presets, live range readout |
| `CompassRose.tsx` | Graduated bearing scale — cardinals, numerals every 30°, ticks every 10° |
| `RangeRings.tsx` | Distance rings and their bearing ticks |
| `ZoneOverlay.tsx` | Site zones and the protected radius |
| `SensorMarkers.tsx` | Sensor placement and boresight |
| `TrackMarker.tsx` | One track's marker, label and data block |
| `PredictedPath.tsx` | Straight-line velocity projection, short or extended-with-ticks |
| `TrackTrail.tsx` | The selected track's recorded route, fading with age |
| `MapDisplayOptions.tsx` / `useMapDisplayOptions.ts` | The data-block field toggle and its persisted state |

## Optional basemap

A geographic layer can be shown beneath the display (ADR 0014). Off by default, and unmounted
when off — so the default view keeps the PPI's defining property of having no tile dependency,
and never even downloads the map library.

Three things to know before changing it:

- **The PPI drives; the basemap follows.** Every MapLibre interaction handler is disabled. A
  basemap that could be dragged independently would slide out of register with the tracks drawn
  over it, silently, and an operator would read that as the *tracks* having moved.
- **The projection is computed, not assumed.** Web Mercator's scale factor is `1 / cos(lat)` —
  1.41× at 45°. Getting it wrong still renders: tiles load, roads look like roads, and every
  track sits somewhere it is not. `basemapCamera.ts` is a pure function with exact tests for
  that reason.
- **Missing tiles are reported, not painted over.** A grey rectangle under the tracks would say
  "there is nothing on the ground there", which is a claim about the world rather than about
  the software. See `docs/basemap.md` for installation.

## Interface boundary

Renders backend state. Computes no track and decides no track state. Range and bearing shown
here — including the live cursor readout — are derived from geodetic position relative to the
*site*, and are labelled as such because no sensor measured them. Coverage arcs are absent
because the API cannot say what they are.

Grid must not be mistakable for data. The bearing reference was once eight full-length radial
spokes from the centre, which is the same shape as an inbound track's trail: an object
approaching on a bearing near one of them (314° against the spoke at 315°) laid the two almost
on top of each other and read as a triangle drawn deliberately around the track. Bearing marks
are now ticks crossing each ring, which carry the same reference and cannot be confused with a
path. There is deliberately **no sweep animation** — a rotating sweep implies a mechanically
scanning radar, and this display is fed by whatever adapters a site has.

The predicted path is the one deliberate extrapolation in this module, and it stays honest
about it: a straight line from the current speed and heading, nothing more. The backend's
Kalman filter has real positional uncertainty internally, but it is not part of the wire
contract, so no confidence cone is drawn — inventing one would imply a calibration that does
not exist. Dashing, dimming and (when selected) explicit `+Ns` time labels mark it as a
projection rather than an observation, the same distinction the backend draws between a
detection and a coasted position.

The history trail is not an extrapolation at all — every point comes from
`GET /tracks/{id}/history`, which is real recorded state. It is fetched only for the selected
track, and only while a track stays selected, which is what keeps deselecting genuinely free
rather than merely hiding a panel while the map stays busy underneath.
