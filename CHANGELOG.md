# Changelog

## 0.8.2 (2026-11-01)

**Fixed**: single unconfirmed returns no longer flicker into the operator's track list. The map
already hid them; the list, the count and the selection now agree with it. One drone now shows
as one row in 94 % of half-second samples, up from 43 %.

**Fixed**: of two duplicate tracks, the one retired is the newer one by creation order, not by a
sensor timestamp that clock skew could reverse. The duplicate test went from 155 ms to 5 ms per
cycle at the 200-track ceiling, with a test pinning that it decides exactly what the old one did.

**Added**: GPL-3.0-or-later licence, this changelog, a security policy and CI for the backend,
the console and the twin.

## 0.8.1 (2026-10-22)

**Docs**: the README is shorter and plainer, leading with the pitch and the quickstart.

## 0.8.0 (2026-10-21)

**Added**: when two tracks are fed by one stream of measurements, the newer is retired and its
risk and open alert carry over to the survivor (ADR 0021). The console follows the retirement,
moving the operator's selection to the survivor.

**Fixed**: dropped clutter was logged as `TRACK_CONFIRMED` then `TRACK_STALE`: 206 false
confirmations for one drone in five minutes of a live run. Unsupported new tracks now go
straight to `LOST`.

## 0.7.0 (2026-10-20)

**Added**: detections of one object from several sensors are reconciled before tracking, so one
drone seen by three sensors becomes one track (ADR 0020).

**Added**: a public front page, with the brand carried through the console.

**Added**: `twin/`, a standalone 3-D digital-twin prototype, and the product vision
([docs/vision.md](docs/vision.md)).

## 0.6.1 (2026-10-20)

**Fixed**: concurrent logged-in requests no longer collide on one database connection, and the
"frontend not built" page no longer crashes on the one request it exists to handle.

## 0.6.0 (2026-08-19)

**Added**: a system-health watchdog that reports a silent sensor as a fact, not a fault
(ADR 0019).

**Fixed**: a pass over the whole repository. One failing sensor no longer costs every sensor
its cycle; risk and classification share one vertical datum, so low-altitude rules work away
from sea level; staleness is measured on one clock; track history is bounded and truncated from
the right end; uncertainty the filter can't use is rejected at ingestion.

**Security**: cross-origin writes are refused, and an offensive pass against the network
surface was closed out. What defends what is in [docs/deployment.md](docs/deployment.md).

## 0.5.0 (2026-08-18)

**Added**: a real testbed radar behind the existing sensor adapter contract (ADR 0017), and
`./start.sh --live` to run the whole system against it (ADR 0018). Modes that would mislabel
live or simulated data are refused.

## 0.4.0 (2026-08-10)

**Added**: risk levels promote into alerts an operator acknowledges (ADR 0015).

**Added**: accounts, roles (viewer, operator, admin), sessions, lockout and an audit trail.
Every route and the event stream require a login (ADR 0016). Deployment guide in
[docs/deployment.md](docs/deployment.md).

## 0.3.0 (2026-08-10)

**Added**: an optional self-hosted basemap beneath the radar display, installed with one
command, `./install-basemap.sh` (ADR 0014). The demo site is centred on Cork.

## 0.2.0 (2026-08-09)

**Added**: tracks are classified from pooled, weighted evidence (ADR 0012), and threat is scored
from nine factors with reasons and hysteresis (ADR 0013), including what several tracks mean
together. The console shows the class distribution and every factor it checked.

**Added**: track history trails, extended prediction, continuous zoom and pan, and a proper PPI
radar face.

## 0.1.0 (2026-08-09)

**Added**: the pipeline from detection to recommendation: ingestion, association, Kalman
tracking in a local ENU frame, zones, decision and an append-only event store that every view
is rebuilt from. Built-in scenarios, the operator console, and `./start.sh` to set up and
run everything with one command, on Linux, macOS and Windows (Git Bash).
