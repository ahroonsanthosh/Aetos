# Installing the geographic basemap

The operational display is a plan position indicator and needs nothing to render (ADR 0010).
An optional basemap can be shown *beneath* it for terrain context (ADR 0014). This is how to
install one.

**Nothing here is required.** With no tiles installed the Terrain button reports "No terrain"
and explains why, and the display is exactly as it would be otherwise.

## What you need

Two files, both served from the frontend's `public/` directory:

| File | Purpose | Required |
|---|---|---|
| `frontend/public/basemap.pmtiles` | The map itself | Yes |
| `frontend/public/basemap-fonts/{fontstack}/{range}.pbf` | Glyphs for map labels | No — without them the terrain draws unlabelled |

Both are copied into the build by Vite and served by the backend's static mount, so a
deployment installs terrain by **copying a file**. No tile server, no API key, no account, and
no network at runtime — which is the property that made this worth doing at all for a system
that has to work air-gapped.

## Getting the tiles

[Protomaps](https://protomaps.com/) builds OpenStreetMap into single-file `.pmtiles` archives.

**An area extract** — what you almost certainly want. A city-sized region is tens of megabytes
against roughly 120 GB for the planet:

```bash
# Install the pmtiles CLI: https://github.com/protomaps/go-pmtiles/releases
pmtiles extract https://build.protomaps.com/20240101.pmtiles frontend/public/basemap.pmtiles \
  --bbox=-6.5,53.2,-6.0,53.5 --maxzoom=14
```

Set the bounding box around your site, generously — an operator can pan two range-rings past
the outer ring, and beyond the extract the map is simply blank.

`--maxzoom=14` is a deliberate ceiling. The display's closest range is 200 m, where zoom 14
gives roughly 10 m per pixel; going deeper multiplies the archive size for detail that a
tactical display does not use.

## Getting the fonts

Optional. Without them the terrain renders and labels do not.

```bash
curl -L -o fonts.zip https://github.com/protomaps/basemaps-assets/archive/refs/heads/main.zip
unzip fonts.zip 'basemaps-assets-main/fonts/*'
mv basemaps-assets-main/fonts frontend/public/basemap-fonts
```

## Checking it worked

Rebuild the interface (`./start.sh` does it), open the display, and click **Terrain**.

- **Terrain appears** — done.
- **"No terrain"** — hover the button. It says which of these it is:
  - *No tile archive installed at /basemap.pmtiles* — the file is missing, or is not a PMTiles
    archive. The check reads the file's first seven bytes rather than trusting the status code,
    because the backend serves the app shell for any unmatched path and a missing file
    otherwise returns a perfectly healthy-looking `200`.
  - *served without range support* — something in front of the backend is stripping `Range`
    headers. A `.pmtiles` archive is read by byte range and never downloaded whole, so this has
    to work.

## Why not Google Maps, or a hosted tile service

Both were considered. See [ADR 0014](adr/0014-optional-basemap.md) — briefly: an API key in a
browser bundle, a billing account, terms that generally forbid caching tiles for offline use,
and a runtime network dependency in a system built to work without one.
