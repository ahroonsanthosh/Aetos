# Brand source

`aetos-logo-source.png` is the logo as supplied — 752×564, flat colour, **no alpha channel**,
composited onto a `#1a171b` rectangle. It is kept here as the source of record. It is *not*
served: this directory sits outside `public/`, so Vite never copies it into a build.

`public/aetos-mark.svg` is the eagle traced from it, and is what the interface actually uses.

## Why it was traced rather than used directly

Three reasons, in order of how much they mattered:

1. **No alpha.** Composited on `#1a171b`, the PNG shows a visible rectangle against the
   interface's `#0a0709` ground wherever it is placed.
2. **One asset, two jobs.** The brief needs the mark large and central on the landing page and
   small in the dashboard nav. A 752 px raster softens at the first and wastes bytes at the
   second; the vector is exact at both.
3. **It has to recolour.** The mark appears in mid purple, in highlight purple, and near-white
   depending on context. The SVG fills with `currentColor`, so it inherits from CSS.

Traced with `potracer` (pure-Python potrace) from a 4× upsampled mask of the `#6B4A6B` pixels,
at `turdsize=8, alphamax=1.0, opttolerance=0.2`, then emitted in source-pixel units. The
canvas-edge frame potrace generates around the padding is dropped by bounding box.

## The wordmark is type, not a trace

"AETOS" in the source is 259 pixels of light grey. It is set in Space Grotesk with wide tracking
instead of being traced — live text is sharp at every size, selectable, searchable, and readable
by a screen reader, none of which a traced raster of that size would be.
