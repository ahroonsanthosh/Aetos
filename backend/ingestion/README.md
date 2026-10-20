# Ingestion

**The boundary every detection crosses on its way into the system.**

One entry point, so that validation, deduplication and the write-before-act rule happen in
exactly one place rather than being re-implemented — or forgotten — by each consumer.

## The four steps

Every detection passes through these in order, and nothing downstream repeats any of them.

1. **Deduplicate.** The same `detection_id` arriving twice is recorded once. A repeat that
   reached the filter would double-weight one observation and make the estimate falsely
   confident (FM-013). The window is bounded (`DEFAULT_DEDUPE_WINDOW`); a duplicate arriving
   later than that is a different problem from a retransmission.
2. **Validate.** Bounds and timestamp sanity beyond what the schema enforces, including a
   clock-skew limit. A rejection is *recorded* as an event with its reason, never dropped
   silently — a sensor producing bad data is itself a fact worth being able to reconstruct
   (FM-011).
3. **Record.** A `DETECTION_RECEIVED` event carrying the whole detection, appended **before**
   anything acts on it. This is what makes the log the source of truth rather than a summary
   of conclusions (ADR 0006), and it is what `GET /detections` reads.
4. **Reduce.** Convert to the tracker's input — position, uncertainty, time, sensor — and
   nothing else.

## Why step 4 matters most

Speed, heading and classification are dropped from the tracker's input here and never cross
that boundary. `IncomingDetection` has no field to carry them.

This is ADR 0002 made structural rather than contractual. The tracker is not *asked* to
ignore fields a real sensor may not supply; it is handed a type in which they do not exist.
The guard test in `tests/integration/test_pipeline.py` runs the same scenario with and
without the simulator's privileged fields and asserts the resulting track state is identical
— a claim that holds because those fields cannot reach the filter, not because the filter
chooses not to read them.

## Hints go sideways, not downstream

Classification hints are dropped from the tracker's input but returned separately on
`IngestionResult.hints`, keyed by detection id.

That is the distinction ADR 0002 actually draws: a sensor's opinion about what an object is
must not influence *where* the system thinks it is, and is perfectly legitimate evidence for
*what* it is. The caller routes those hints to tracks using the association the tracker
produced — see `backend/classification/service.py`.

## What it returns

```python
IngestionResult(
    for_tracking=[...],  # reduced detections, the tracker's only input
    hints={...},  # detection_id -> ClassificationHint
    quality={...},  # detection_id -> quality confidence
    accepted=int,
    rejected=int,
    duplicates=int,
)
```

Mutable, and honestly so: it accumulates over one cycle's detections. A frozen wrapper around
mutable lists would advertise a guarantee it could not keep.

## Files

| File | Purpose |
|---|---|
| `pipeline.py` | `IngestionPipeline`, `IngestionResult` |

Tests: `tests/unit/test_ingestion.py`, plus the provenance guard in
`tests/integration/test_pipeline.py`.
