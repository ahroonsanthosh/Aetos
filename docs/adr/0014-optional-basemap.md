# ADR 0014 — An optional self-hosted basemap beneath the PPI

- **Status:** Accepted
- **Date:** 2026-08-09
- **Extends:** [ADR 0010](0010-radar-ppi-display.md), which chose the PPI and anticipated this:
  *"If situating a site in real terrain becomes a requirement, the separation between track
  rendering and display substrate is what makes a second view addable."* That requirement has
  now been asked for. ADR 0010's decision stands; this adds a layer beneath it.

## Context

The PPI answers "how far away and in what direction, relative to what we are defending". It
does not answer "what is on the ground there" — whether an approach corridor follows a road, a
river, a treeline; whether the object holding station is over a car park or a school.

The specific request was for the Google Maps API. Three options were weighed.

**Google Maps JavaScript API.** Best imagery, and the most costly in every other dimension. It
needs a billing account and an API key that ships in the browser bundle — domain-restrictable,
never secret. Its terms generally prohibit caching tiles for offline use, which removes the
property ADR 0010 was built around. And for a defence-adjacent product the licence terms are
something to have checked rather than assumed.

**A hosted tile service** (MapTiler, Mapbox, raw OSM). Cheaper and simpler, still a runtime
network dependency, still an account for most of them.

**MapLibre GL with a self-hosted PMTiles archive.** Chosen. Open source, no key, no account,
no billing. A PMTiles archive is a single file read by HTTP byte range; drop it in `public/`
and the backend's existing static mount serves it. A deployment installs terrain by copying a
file, and everything still works with no network at all.

## Decision

A geographic basemap may be shown **beneath** the PPI. Four constraints define it.

### The PPI stays authoritative

The basemap follows the PPI's viewport and never drives it. Every MapLibre interaction handler
is disabled. A basemap that could be dragged independently would slide out of register with
the tracks drawn over it — silently, and in a way an operator would read as the *tracks* having
moved.

### Off by default, and unmounted when off

Nothing about the default display changes, including its lack of any tile dependency. MapLibre
is imported dynamically, so a session that never turns terrain on never downloads the ~980 kB
library: the main bundle is essentially unchanged.

### Absent tiles are reported, never painted over

Without an archive the layer renders nothing and the control says "No terrain" with the reason.
A blank grey rectangle under the tracks would read as *there is nothing on the ground there* —
a statement about the world rather than about the software, and a false one. This is ADR 0011's
rule applied to the display substrate.

The check reads the archive's first seven bytes rather than trusting a status code, because the
backend serves the app shell for any unmatched path: a missing file returns a healthy-looking
`200`, or a `206` if a range was requested. A status-only probe would hand MapLibre a page of
HTML to parse as a tile archive and then report the failure as an obscure rendering error.

### The projection is computed, not assumed

Web Mercator's scale factor is `1 / cos(latitude)` — at 45° that is 1.41×. A basemap laid under
an ENU-metre display without accounting for it still renders: the tiles load, the roads look
like roads, and every track sits somewhere it is not. That failure is silent and an operator
cannot detect it from the display, so the conversion is a pure function with exact unit tests
(`basemapCamera.ts`, `tests/unit/basemapCamera.test.ts`) rather than something eyeballed against
a screenshot.

## Consequences

- Three new frontend dependencies (`maplibre-gl`, `pmtiles`, `@protomaps/basemaps`), all
  code-split behind the toggle.
- `LocalFrame` gained `toGeodetic`, the exact inverse of `toEnu`. The basemap needs a centre in
  degrees while everything else works in metres from the site.
- MapLibre derives its worker URL at runtime from `import.meta.url`, which a bundler cannot see,
  so the worker is requested explicitly through Vite and installed with `setWorkerUrl`. Without
  that the worker request falls through to the app shell, a module worker is handed HTML, and
  the basemap reports no error and never finishes loading.
- Terrain is a deployment concern with its own installation step, documented in
  `docs/basemap.md`. It is deliberately not vendored: an area extract is tens of megabytes and
  belongs to a deployment rather than to the source tree.
- The offline guarantee is preserved but is now conditional on *how* a deployment installs
  tiles. Pointing the archive or the glyph URL at a remote host would reintroduce the network
  dependency this avoided; both default to local paths for that reason.

## Alternatives considered

- **Replace the PPI with a slippy map.** Rejected for every reason in ADR 0010, none of which
  has changed. Range rings and a bearing scale are the PPI's native coordinate system and
  become distorting overlays on a Mercator basemap.
- **Google Maps.** Rejected above. Available imagery does not outweigh a browser-exposed key, a
  billing dependency, a runtime network requirement, and licence terms that would need review.
- **Vendor a small tile extract in the repository.** Rejected: the correct extract is
  site-specific, and a repository is the wrong place for tens of megabytes of derived
  geographic data.
