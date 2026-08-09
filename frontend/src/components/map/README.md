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

| File | Responsibility |
|---|---|
| `OperationalMap.tsx` | Layout, the SVG viewport, range control, wires selection to history |
| `RangeRings.tsx` | Distance rings, bearing grid, cardinal marks |
| `ZoneOverlay.tsx` | Site zones and the protected radius |
| `SensorMarkers.tsx` | Sensor placement and boresight |
| `TrackMarker.tsx` | One track's marker, label and data block |
| `PredictedPath.tsx` | Straight-line velocity projection, short or extended-with-ticks |
| `TrackTrail.tsx` | The selected track's recorded route, fading with age |
| `MapDisplayOptions.tsx` / `useMapDisplayOptions.ts` | The data-block field toggle and its persisted state |

## Interface boundary

Renders backend state. Computes no track and decides no track state. Range and bearing shown
here are derived from geodetic position relative to the *site*, and are labelled as such
because no sensor measured them. Coverage arcs are absent because the API cannot say what
they are.

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
