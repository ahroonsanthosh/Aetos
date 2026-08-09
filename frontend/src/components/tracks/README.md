# `components/tracks`

## Responsibility

Track list and selected-track detail. `HistorySparkline` is a small, generic
value-over-time chart (hand-drawn SVG, no charting library) used by the Track History page to
show altitude and speed across a track's recorded life.

## Interface boundary

Displays what the backend sends. A field the backend did not supply renders as an em dash —
never interpolated, never carried forward.

`HistorySparkline` positions points by elapsed time, not by array index: history is one point
per recorded event, not a fixed-interval sample, so index-based spacing would misrepresent a
track that went quiet for a while as one that was changing quickly throughout.
