# Aetos Command — holographic digital twin

A 3D command interface for a protected site: the facility as a digital twin, the airspace
above it, what is in that airspace, what it threatens, and how well the system can
currently see and protect the place.

**Standalone prototype.** It runs on its own simulator and is not wired into the Aetos
backend. It is developed here, on its own, until it is good enough to be worth integrating
— see [Integrating with Aetos Core](#integrating-with-aetos-core).

```bash
cd twin
npm install
npm run dev        # http://localhost:3000
npm test           # simulation layer
npm run typecheck
```

Desktop-first. It expects a command-centre display or a laptop, and a GPU.

## What it does today

- A demonstration solar facility — substation, transformers, control building, battery
  storage, inverters, six solar array blocks, roads, perimeter fencing, five sensor masts.
- Protected zones as **volumes** with real ceilings, not circles on a map.
- Simulated UAV tracks with observed history and an extrapolated prediction.
- A threat model that is **asset-aware**: a bird over the substation stays LOW; an
  unidentified UAV whose predicted path enters the substation volume does not.
- Sensor coverage envelopes, a site coverage map, per-asset protection scores and an
  overall readiness figure — all derived from one model, so a sensor failure moves all of
  them together.
- Ten scenarios, switchable live, including sensor failure, coverage gaps, weather
  degradation and a false-positive bird flock.
- A site editor (`/editor`) that builds a site from real OpenStreetMap footprints, a traced
  reference image, or by hand — see [Using your own site](#using-your-own-site).

### What it does not do

No response control of any kind. The recommendation ladder tops out at **operator review
required**, which is advice addressed to a human. There is no authorise step, no execute
step, and no code path toward one. A test asserts it
(`engine.test.ts` → *never recommends anything beyond operator review*).

All data is synthetic. The interface labels it `SYNTHETIC DATA` permanently, in the
simulation controls.

## Architecture

The rule the whole thing is built around:

> The 3D scene and the interface consume `Snapshot`s. Neither can tell where a snapshot
> came from.

```
SimulationEngine ──┐
                   ├──> DataSource ──> snapshot buffer ──> 3D scene
WebSocket (later) ─┘         │                        └──> command UI
                             └──> Zustand store
```

| Path | What lives there |
|---|---|
| `src/types/` | The domain contract. Site, Asset, Sensor, Zone, Track, Threat, ResponseResource, SystemHealth, Snapshot. |
| `src/site/` | The site: the demonstration facility as data, the file schema, scene scaling, default sensor/zone generation, and the OSM and image-tracing importers. |
| `src/ui/editor/` | The site editor — trace canvas, view transform, forms. |
| `src/simulation/` | Flight paths, threat model, coverage model, weather, scenarios, engine. No DOM, no three.js. |
| `src/state/` | `DataSource` (the swap seam), the snapshot interpolation buffer, the store. |
| `src/three/` | Scene, camera, materials, site geometry, sensors, zones, tracks. |
| `src/ui/` | Top bar, threat rail, context panel, mode and simulation controls. |
| `src/lib/` | Geometry and the shared palette. |

### Three decisions worth knowing about

**Snapshots at 10 Hz, rendering at 60.** The simulator publishes at a fixed rate, exactly
as a real feed would, and `state/buffer.ts` interpolates between the last two snapshots one
tick in the past. Track positions are read per-frame from that buffer and never pass
through React. A simulator that emitted a position every frame would let the scene become
dependent on frame-rate data, and the swap to a real feed would then look broken.

**The domain frame is ENU, not three.js.** Positions are east/north/up metres from the site
datum, matching the tracking frame the Aetos backend uses (ADR 0004), so Core positions
can be consumed unmodified. The single conversion to renderer axes is in `three/frame.ts`.

**Threat carries its own reasons.** `ThreatAssessment.factors` travels with the score, so
the panel can always answer "why is this high?" without recomputing anything. A score the
interface cannot explain is an oracle, not an assessment.

## Scenarios

| Scenario | What it demonstrates |
|---|---|
| Reset | Quiet sky, all sensors nominal. |
| Single UAV | Detected, tracked, correctly left alone. |
| UAV Approaching | Closes on the perimeter, then withdraws. |
| Critical Asset Approach | Threat rises with the trajectory, not the range. |
| UAV Departing | Threat decays as the geometry opens. |
| Multiple UAVs | Four tracks with different intent; one authorised. |
| False Positive | A bird flock over the substation that stays LOW. |
| Sensor Failure | RF offline, camera degraded; readiness and protection fall together. |
| Coverage Degradation | South-west radar down, something approaching through the gap. |
| Weather Degradation | Fog: optics blind, radar unaffected, and the system knows the difference. |

The one to look at is **False Positive**. Turning something red when it gets close is easy;
staying calm when the object is close and harmless is the harder and more valuable
behaviour, and it is the whole argument for being asset-aware.

## Using your own site

The demonstration facility is data, not code. There are three ways to replace it, and the
intended use is combining them rather than picking one.

Open the **site editor** at `/editor`, or from the "Site editor" link in the command view's
top bar.

### 1. Import from OpenStreetMap

Enter a latitude, longitude and radius; the editor queries the Overpass API for real
building footprints and turns each into an asset with its true outline and, where the
building is tagged with a height or a floor count, its real height.

Free and keyless. **Not Google Maps** — Google's satellite tiles and Places data need a paid
API key this project does not have. OSM is the legitimate equivalent, with a real
limitation: coverage is patchy outside urban areas, and an untagged building arrives with a
guessed height for you to correct.

### 2. Trace a reference image

Upload a satellite screenshot or a site plan, then calibrate it: click two points whose
real-world distance you know, and type that distance. From that one measurement the image
acquires a metres-per-pixel scale, and everything you trace on top of it lands in real
coordinates.

**There is no automatic building extraction.** Pulling outlines out of an image is a
computer-vision problem this project does not solve, and pretending otherwise would be
dishonest about what the tool does. It reads what you draw. In practice tracing a facility's
dozen significant buildings takes a few minutes and is more accurate than an OSM import,
because you decide what counts as an asset.

The image is placed north-up. `Calibration.northBearing` carries a rotation and
`pixelToGround` applies it correctly to traced points, but the raster preview underneath
stays axis-aligned — a deliberate simplification, since most screenshots are already
north-up.

### 3. Place and edit by hand

"Place asset" drops a small asset wherever you click. Anything selected can have its name,
type, criticality and dimensions edited in the right-hand panel.

### Then: sensors, zones, and out

One button generates a starting layout — sensors spaced around the boundary aimed inward, a
perimeter zone, and a protected volume around every asset at criticality 6 or above. It is a
starting point to adjust, not a design.

"Download site.json" writes a validated site file. "Preview in Command" loads it straight
into the command view. Either output can be loaded again later from the command view's
"Load site…" control or the editor's "Load / continue editing".

### Asset types

Seven types have bespoke 3D models: `substation`, `transformer`, `inverter`,
`control_building`, `solar_array`, `storage`, `gate`. **Any other type is still valid** —
`AssetType` is an open string, and anything unrecognised renders through `GenericAsset` as
massing built from its footprint, extruding a traced outline where one exists. A site
imported from real building data is never invisible for using vocabulary this codebase has
not been taught.

### What scales automatically

Camera distances, ground plane size, fog, grid spacing and coverage sampling all derive from
the loaded site's own extent (`src/site/presentation.ts`), so a 200 m compound and a 4 km
refinery are both framed sensibly with nothing to retune by hand.

Camera framing uses the *physical* extent — boundary and assets. The ground plane and zoom
limits use the *scene* extent, which also covers sensor range. A 100 m compound with a 3 km
radar is a 100 m site to look at, and a 3 km world to draw.

### What does not carry over

The ten built-in scenarios are flight paths written against the demo site's own geometry.
Loading a different site resets the scenario to "Reset" rather than flying a scripted UAV
through buildings that were never there. Scenario authoring for arbitrary sites is not built.

## Integrating with Aetos Core

One line, in `src/ui/CommandInterface.tsx`:

```ts
return attachSource(new SimulationSource(DEMO_SITE, DEFAULT_SCENARIO_ID));
// becomes
return attachSource(new WebSocketSource('wss://…/airspace'));
```

`WebSocketSource` is already written (`src/state/source.ts`) — untested against a server,
because Core does not publish this feed yet. It exists so the seam is real rather than
asserted: if the interface could not be satisfied by a socket, the abstraction would be
decoration.

What Core would need to publish is the `Snapshot` shape in `src/types/index.ts`. Getting
there means the backend growing the pieces the twin currently simulates — `backend/fusion/`
and `backend/system_health/` are both empty today, and there is no coverage model. Until
then this stays a prototype, and says so.

The site itself is the other half: `src/site/demo-site.ts` is a fictional facility written
by hand. Real deployment means that file becoming a site survey — asset positions,
criticalities, zone polygons, sensor placements — loaded rather than compiled in.

## Testing

```bash
npm test
```

114 tests, all free of DOM and three.js so the parts carrying the reasoning can be verified
without rendering anything. They pin the behaviour the interface exists to demonstrate:
trajectory beats range, birds stay low, losing a sensor moves coverage and protection and
readiness together, and the recommendation ladder never exceeds operator review.

The site pipeline is covered to the same standard, because it is all pure geometry and
therefore all checkable: lat/lon to ENU round trips, the image calibration rotation against
hand-derived expectations at four bearings, Overpass parsing against a fixed fixture, hull
and buffer containment, scene scaling, and schema validation including the open asset type.

The rendering is not unit-tested. It is checked by looking at it.
