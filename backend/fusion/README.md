# `backend/fusion` — not built

This package is empty on purpose, and the empty `__init__.py` is the only file in it.

## What would live here

Reconciling multiple sensors' observations of **one object** into one track. Today two sensors
watching the same drone produce two tracks, because association runs per detection without any
notion that two detections from different sensors might be the same thing seen twice.

## Why it is empty rather than absent

`docs/architecture.md` lists fusion as a pipeline stage and the module map names it. A directory
that exists and is empty says "known, not built"; a directory that does not exist at all says
nothing, and the next person to look would have to read the architecture document to find out
whether they had missed something.

## What its absence currently costs

**FM-023** — one object becomes several tracks under multi-sensor observation. The operator sees
a track count higher than the object count. `backend/risk/correlation.py` mitigates the *display*
consequence by clustering tracks that are probably one object and reporting "8 tracks · likely 5
objects", but that is a correction applied downstream, not a fix: the tracks themselves remain
separate, each with its own identity, history and risk score.

**FM-024** — classification sees only one sensor's hint per track, because the hints are split
across the tracks the same object produced.

## Before building it

Read ADR 0005 (association) and `backend/risk/correlation.py`, which already answers "are these
probably the same object" for a different purpose. Two implementations of that question that can
disagree would be worse than the current gap.
