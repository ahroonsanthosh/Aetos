# Aetos — operator interface

The frontend for the Aetos Counter-UAS platform. It visualises and interacts with the
backend's output; it does not reimplement tracking, fusion, classification or risk.

## Running it

The interface needs a backend to talk to. From the repository root:

```bash
# Terminal 1 — backend, driving a scenario so there is something to see
python -m backend.main --scenario b_approaching_threat

# Terminal 2 — interface
cd frontend && npm install && npm run dev
```

Then open http://localhost:5173. The dev server proxies `/api` to `http://127.0.0.1:8000`;
set `AETOS_BACKEND` to point elsewhere.

Available scenarios are the nine in `scenarios/` — `a_benign_transit`,
`b_approaching_threat`, `c_ambiguous_intermittent`, `d_multiple_simultaneous`,
`e_sensor_degradation`, `f_false_detections`, `g_sensor_disagreement`, `h_track_crossing`,
`i_swarm_saturation`.

```bash
npm run build      # typecheck and bundle
npm run typecheck  # types only
npm test           # unit tests
```

## The rules this interface is built on

**It renders only what the backend actually knows** ([ADR 0011](../docs/adr/0011-frontend-honest-rendering.md)).
A missing field is an em dash, never an interpolated value. An unbuilt subsystem says
`NOT BUILT` rather than showing green. An empty alert feed says alerting is not implemented
rather than reading as "all clear". This is not caution for its own sake: the whole output of
this system is a judgement an operator is asked to trust, and an interface that fabricates
plausible values to look finished teaches them to believe a display that sometimes lies.

**It owns no authoritative state.** The picture it holds is a cache of backend state. Nothing
here computes a track, decides a track's state, or infers a value the backend did not send.
Where the two disagree, the backend is right by definition.

**Simulated data is never presentable as real.** The mode indicator reads `GET /system/mode`,
where `simulated` is computed from whether a real adapter is actually feeding the pipeline —
not from a config string, and never from a frontend constant.

**Aetos assessment and human decision are always separate elements.** They are never merged
into one card, even when there is no assessment to show. Aetos produces advice; the operator
is the authority.

**Nothing here acts on the physical world.** The Response Systems page is architecture and
interface only, permanently `NOT CONNECTED`. See [`response/README.md`](../response/README.md).

## How live data arrives

```
GET /tracks          →  snapshot + as_of_seq
WS  /stream?from_seq=as_of_seq+1  →  every subsequent event, in order
```

Sequence numbers rather than timestamps make the handover exact: `seq` is a total order
assigned by a single writer, so there is no gap and no duplicate at the boundary, and a
reconnecting client resumes at one past the highest sequence it applied. Sensor clocks can
disagree; the sequence cannot.

Every event that changes a track carries the full resulting track, so the client replaces
state rather than patching it — patching would mean reimplementing the backend's logic on the
client and drifting from it.

## Layout

| Path | Contents |
|---|---|
| `src/app/` | Shell routing and pages |
| `src/components/map/` | PPI display: rings, zones, sensors, track markers |
| `src/components/tracks/` | Track list and detail |
| `src/components/assessment/` | Assessment, and the operator-decision separation |
| `src/components/shell/` | Top bar, sidebar |
| `src/data/models/` | Wire types (`api.ts`), presentation types (`track.ts`), geodesy |
| `src/data/client/` | REST client, event stream, live picture hook |
| `src/styles/` | Design tokens — see [design_tokens.md](../docs/design_tokens.md) |

The operational map is a site-centred plan position indicator rather than a geographic
basemap ([ADR 0010](../docs/adr/0010-radar-ppi-display.md)). It has no external tile
dependency and renders identically offline.

## Known limitations

- **One object can briefly appear as two tracks.** Fusion removes most multi-sensor duplicates
  before tracking, and the tracker retires what it leaves once the evidence is in — about five
  seconds of the two tracks taking turns (`docs/failure_modes.md` FM-023, ADR 0021). The
  interface does not hide the duplicate in the meantime. When it is retired, a selection on it
  moves to the track that carries on (`src/data/models/succession.ts`).
- **Sensor runtime status is not broken out per sensor.** The watchdog judges every source and
  the System Health page shows the result and the overall posture, but the Sensors page shows
  placement only. Per-sensor status there is not invented.
- **The predicted path is a straight line**, current speed and heading held constant, nothing
  more. The backend has real positional uncertainty internally but does not expose it over
  the API, so no confidence cone is drawn — an invented one would claim a calibration that
  does not exist. See `components/map/PredictedPath.tsx`.
- **A track that has left the live picture can be looked up on the Track History page, but
  not discovered there.** History is retained permanently and reachable by ID, but no
  endpoint enumerates every ID the system has ever created — so a LOST track needs to be
  looked up by an ID already known (from the Event Log, for instance), not found by browsing.
- **The console requires a sign-in.** The operator identity comes from a validated session,
  and the role decides what the interface *offers* — never what it permits, which the backend
  decides on every request (ADR 0016).
