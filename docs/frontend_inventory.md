# Frontend inventory — the pre-redesign baseline

**What this is.** A complete record of the operator interface as it stood *before* the landing
page and visual redesign, taken so that every data point, every control and every backend
contact point can be checked off afterwards. Nothing in a redesign is allowed to quietly
disappear, and "I think it's all still there" is not a verification.

Taken 2026-08-19 against `claude/examine-codebase-sck2hv` at commit `582db09`.

---

## 1. Landing page

**None exists.** There is no public-facing page of any kind. `/` is the operator's Overview and
is behind authentication; an unauthenticated visitor gets the login screen and nothing else.
`frontend/index.html` has one mount point, an inline-SVG favicon of a PPI scope, and the title
*AETOS — Counter-UAS / Sensor Fusion*.

There is also **no `public/` directory and no image, font or icon file anywhere in the
repository.** Every visual so far is drawn in CSS or SVG at runtime. Any logo, photograph or
texture the new landing page needs is a new asset with no precedent to match.

## 2. Stack

Fixed by what is already here; the landing page joins it rather than arriving beside it.

| Concern | What is used |
|---|---|
| Framework | React **18.3.1**, TypeScript **5.6.3** |
| Build | Vite **5.4.9** (`tsc -b && vite build`) |
| Routing | `react-router-dom` **6.26.2**, `BrowserRouter`, SPA fallback served by the backend |
| Map | `maplibre-gl` **6.2**, `@protomaps/basemaps`, `pmtiles` — optional local basemap (ADR 0014) |
| Styling | **Plain CSS, one file per component**, all values from custom properties in `src/styles/tokens.css` |
| Component library | **None.** Every control is hand-written. |
| State | React hooks and one context (`auth.tsx`). No Redux, Zustand, or query library. |
| Tests | Vitest **2.1.3** + `@testing-library/react`, jsdom. 56 tests. |

### The styling rule that constrains this redesign

`src/styles/tokens.css` opens with: *"Every colour, size and spacing value in the interface comes
from here. A component that writes a literal colour is a bug."* Its palette is deliberately
near-achromatic on the stated grounds that **colour is a status signal, never decoration** — if
amber or red is on screen it means something, and an interface that uses colour freely destroys
the reflex that makes a warning colour work.

That rule is why the redesign is executed **as token values**, not as new literal colours in new
components. It is also the rule most at risk from a purple accent: the accent must not end up
meaning "decorative" in one place and "alert" in another.

> Two files reference `docs/design_tokens.md`, which **does not exist** (`tokens.css:5`,
> `frontend/README.md:83`). Broken link, found during this audit.

## 3. Routes and pages

Ten screens. `App.tsx` gates everything: nothing renders and **no WebSocket opens** until the
session is known — `useTrackPicture` lives in a child component specifically so a hook cannot
open the live picture behind the login screen.

| Route | Page | Purpose |
|---|---|---|
| *(pre-auth)* | `LoginPage` | Username/password. Carries the SIMULATION/LIVE mode banner deliberately — mode must be visible before anyone logs in. |
| `/` | `OverviewPage` | Situation banner · map · track detail · track list |
| `/tracks` | `TracksPage` | Full track table + detail |
| `/alerts` | `AlertsPage` | Alert feed, acknowledgement |
| `/history` | `TrackHistoryPage` | Per-track history table + sparkline |
| `/sensors` | `SensorsPage` | Placement + watchdog condition |
| `/health` | `SystemHealthPage` | Component states, log/projection lag |
| `/events` | `EventLogPage` | Raw event log, filterable |
| `/response` | `ResponseSystemsPage` | **Deliberately empty** — see §7 |
| `*` | → `/` | Unrecognised URLs return to the picture, not an error page |

Sidebar also lists `/fusion`, `/classification`, `/assessments` as **present but unavailable**,
each with a reason. That is ADR 0011: a navigation that hides unbuilt areas makes "not built" and
"does not exist" indistinguishable.

## 4. Components

31 `.tsx` files, 17 `.css` files, 4671 lines.

**Shell** — `AppShell` (48 px top bar, 216 px sidebar, content), `TopBar` (system/stream/tracks/
fusion chips, mode badge, UTC clock, operator, sign-out), `Sidebar` (3 sections, 10 entries).

**Map** (13 files, the largest area) — `OperationalMap`, `BasemapLayer`, `TrackMarker`,
`TrackTrail`, `PredictedPath`, `RangeRings`, `ZoneOverlay`, `SensorMarkers`, `CompassRose`,
`MapControls`, `MapDisplayOptions`, plus `useMapViewport`, `useBasemap`, `basemapCamera`,
`useMapDisplayOptions`.

**Tracks** — `TrackList`, `TrackDetails` (incl. the fusion roster section), `HistorySparkline`.

**Assessment** — `AssessmentPanel` (classification, risk factors, recommendation),
`SituationBanner` (renders *nothing* when there is nothing to say, by design).

## 5. Every interactive control

Nine in total. All must survive.

| Control | Where | Effect |
|---|---|---|
| Sign in (username, password, submit) | `LoginPage` | `POST /auth/login` |
| Sign out | `TopBar` | `POST /auth/logout` |
| Select track | `TrackList` row, `TrackMarker`, `SituationBanner` link | Lifts `selectedTrackId` to `App` |
| Map: zoom in / out / recentre / north-up / follow | `MapControls` (5 buttons) | Viewport only |
| Map display options toggle | `MapDisplayOptions` | Layer visibility |
| Acknowledge alert | `AlertsPage` | `POST /alerts/{id}/acknowledge` |
| Track selector (`<select>`) | `TrackHistoryPage` | Which track's history |
| History point limit (`<input>`) | `TrackHistoryPage` | Query size |
| Event filter (`<input>`) | `EventLogPage` | Client-side filter |

## 6. Backend contact points — **frozen**

Ten REST calls and one WebSocket, all through `src/data/client/api.ts` and `stream.ts`. No page
calls `fetch` directly. **None of these request/response shapes may change in a UI build.**

| Call | Endpoint |
|---|---|
| `api.tracks` | `GET /tracks` |
| `api.track` | `GET /tracks/{id}` |
| `api.trackHistory` | `GET /tracks/{id}/history` |
| `api.situation` | `GET /situation` |
| `api.alerts` | `GET /alerts` |
| `api.acknowledgeAlert` | `POST /alerts/{id}/acknowledge` |
| `api.sensors` | `GET /sensors` |
| `api.sites` | `GET /sites` |
| `api.scenarios` | `GET /scenarios` |
| `api.mode` | `GET /system/mode` |
| `api.health` | `GET /system/health` |
| `api.login` / `logout` / `me` | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` |
| Live picture | `WS /stream?from_seq=N` |

**The stream is the load-bearing one.** One socket per client, resuming from a sequence number,
established once in `App.tsx` and passed down. Two streams would drift and two pages would then
disagree about what is in the sky. Polling intervals elsewhere: alerts 2 s, health 10 s.

## 7. Things that are deliberately absent, and must stay absent

A redesign is exactly where these get "tidied up" by accident.

- **`ResponseSystemsPage` shows no response systems.** Aetos stops at a human-facing
  recommendation; `response/` is an empty directory behind a design pass. The page exists to say
  so. It must not gain anything that looks like an effector control, and no landing-page copy may
  claim one.
- **"Not assessed" is never rendered as "nothing found."** Every assessed-flag branch in
  `TrackDetails`, `AssessmentPanel`, `AlertsPage`, `SensorsPage` and `SystemHealthPage` exists
  because the two are opposite in meaning and identical on the wire (ADR 0011).
- **`SituationBanner` renders nothing when there is nothing to say.** A permanent
  "SITUATION: NORMAL" bar is one an operator stops seeing.
- **The mode badge is on the login screen**, before authentication, on purpose.
- **No light theme.** Stated as a decision, not a gap: the status palette's contrast is
  calibrated against a near-black ground for low-light operations rooms.

## 8. Blockers and deviations found during the audit

1. **`21st.dev` is unreachable from this environment.** The egress proxy answers 403 to the
   CONNECT tunnel and WebFetch reports `EGRESS_BLOCKED`, so `halide-topo-hero` cannot be fetched
   and its shadcn CLI command cannot resolve. Every mechanism that component was specified for —
   `perspective` + `transform-style: preserve-3d` parallax, mouse-reactive tilt, `feTurbulence`
   grain, topographic contours, staged entrance, HUD monospace — is plain CSS/SVG/React and is
   built directly instead. See §9.
2. **No logo file exists in the repository.** The eagle mark has to be authored as an SVG.
3. **Google Fonts and the npm registry are reachable**; `unpkg` and `jsdelivr` are not. Fonts are
   downloaded and **self-hosted** rather than linked — a defence console should not make a
   third-party request on load, and it has to work air-gapped.
4. **`docs/design_tokens.md` is referenced twice and does not exist.**

## 9. Why shadcn/21st.dev would not have been used even if reachable

Worth recording, because it is a design decision rather than a workaround.

`shadcn` components are Tailwind + Radix + `class-variance-authority`. Installing them would put
a second, utility-class styling system into a codebase whose stated rule is that **a literal
colour in a component is a bug**, and whose entire visual language is one CSS file of tokens. The
brief itself asks for integration "with the same stack, not a separate bolt-on." Two styling
systems in one repository is the bolt-on.

The mechanisms are what carry the visual quality, not the packaging — and they are all standard
platform features. They are implemented directly, against the existing token system.
