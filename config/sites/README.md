# `config/sites/` — the protected geography

## This is fictional test data

`fictional_test_range.json` describes an invented site. It is not a real installation, a real
customer, a real address or a real deployment. Its reference point is a rounded graticule
intersection chosen precisely because no such facility exists there, and every zone and sensor
placement around it was made up to exercise the model. Nothing in this directory should be read
as a claim about any real place.

## Format

Validated against `Site` in `backend/zones/site.py`. Unknown fields are rejected, so a typo is
an error rather than a setting that silently does nothing.

```jsonc
{
  "schema_version": "1.0.0",
  "site_id": "fictional_test_range",   // must equal the file name
  "name": "Aetos Fictional Test Range",

  "reference": {                        // anchor of the local ENU frame (ADR 0004)
    "latitude": 45.0,
    "longitude": 0.0,
    "altitude_m": 120.0                 // metres above the WGS-84 ellipsoid
  },

  "protected_radius_m": 800.0,          // the site's headline distance

  "zones": [ /* see below */ ],
  "sensors": [ /* see below */ ]
}
```

### `reference`

Anchors the tangent plane every downstream calculation happens in. It is immutable for the
lifetime of a running system: changing it silently invalidates every in-flight track, because
track state is stored in the frame it defines. Site extent is bounded by the approximation —
tens of kilometres, not hundreds (FM-002).

`altitude_m` is metres above the **WGS-84 ellipsoid**, not above ground and not above mean sea
level. So are zone altitude bounds. Confusing these is a silent error of tens to hundreds of
metres (FM-004).

### `protected_radius_m`

The primary protected area around the reference point. Kept separate from the zone list because
risk assessment needs one unambiguous distance-to-protected-area even for a site that declares
no explicit protected zone.

### `zones`

```jsonc
{
  "zone_id": "core_protected",          // unique within the site
  "name": "Protected core",
  "kind": "protected",                  // monitored | restricted | protected
  "geometry": { "shape": "circle", "centre": { "latitude": 45.0, "longitude": 0.0 },
                "radius_m": 800.0 },
  "floor_m": null,                      // optional, ellipsoidal metres; null = unbounded below
  "ceiling_m": null                     // optional; null = unbounded above
}
```

`kind` is ordered by consequence: `monitored` < `restricted` < `protected`. Zones are expected
to overlap, and a point inside several is assessed against the most consequential one it
occupies. Where two zones of equal kind overlap, the one declared **earlier** in this list
wins, so ties are the site author's decision rather than an accident of iteration order.

Polygon geometry:

```jsonc
"geometry": {
  "shape": "polygon",
  "vertices": [ { "latitude": …, "longitude": … }, … ]   // three or more, ordered
}
```

The ring closes implicitly. Repeating the first vertex as the last is rejected, so a given
polygon has exactly one representation. Vertices carry no altitude — a zone footprint is a
ground-plane shape, and vertical extent is `floor_m` / `ceiling_m`.

An unbounded ceiling is the usual case: something overflying a protected area at height is
still overflying it. Bound the vertical extent only where the zone genuinely is a slab, such as
a low-level corridor.

A point exactly on a zone boundary — on an edge or on a vertex — counts as **inside**. See
`backend/zones/README.md` for why that direction was chosen.

### `sensors`

```jsonc
{
  "sensor_id": "radar_north",           // unique within the site
  "registry_key": "sim_radar_medium",   // config/sensor_registry/ (simulated) or
                                        // config/hardware/ (a device on a real port)
  "position": { "latitude": …, "longitude": … },
  "height_m": 12.0,                     // above the site reference altitude: mast or rooftop
  "boresight_deg": 0.0,                 // clockwise from true north
  "enabled": true
}
```

Placement only. Range, field of view, update rate, uncertainty, probability of detection and
false alarm rate belong to the sensor registry, because one sensor model is placed at many
sites and one site is fitted with different sensors. `boresight_deg` is ignored by sensor models
with full azimuthal coverage.

Which directory `registry_key` resolves in decides whether the placement is simulated or real —
see `config/hardware/README.md`. For a fitted sensor, `boresight_deg` and `height_m` stop being
bookkeeping and become measurements: they are how a range and a bearing from the device become
a position on the Earth, and an error in either is a systematic error in every detection it
produces.

## The shipped site

Reference at 45.0 N, 0.0 E, 120 m ellipsoidal. Distances below are from the reference point.

| Zone | Kind | Geometry | Vertical |
|---|---|---|---|
| `core_protected` | protected | circle, 800 m | unbounded |
| `inner_restricted` | restricted | circle, 2 500 m | unbounded |
| `outer_monitored` | monitored | circle, 8 000 m | unbounded |
| `eastern_approach_corridor` | restricted | polygon, 2–6 km east, 1.2 km wide | 170–620 m ellipsoidal (50–500 m above the reference) |

Five sensor placements referencing three registry keys, one of them disabled so that the
`enabled` flag is exercised by something other than a test fixture.

Vertex coordinates were generated from intended ENU offsets and rounded to seven decimal places,
which places them within a few millimetres of the round metre figures above. Tests assert
against those figures with tolerance rather than exactly, because the rounding is in the data.
