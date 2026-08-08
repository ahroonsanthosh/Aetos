# `config/environments/` — deployment environments

## Responsibility

One document per environment, naming which site is defended, where detections come from, where
the event log lives, and the tuning that belongs to a deployment rather than to the code.

Environments are separated from the first commit, before there is a second one to separate
from. Retrofitting the split later means every value that was ever tuned for simulation is
already sitting in whatever a staging run reads.

Validated against `EnvironmentConfig` in `backend/common/config.py`. Unknown fields are
rejected. `environment` must equal the file name.

## Read this before deploying anything

**`prod.json` is a placeholder. It does not describe a production deployment and must not be
used as one.**

Aetos has no authentication, no authorisation and no roles (ADR 0008, failure mode FM-001).
Operator actions are attributable in structure but not authenticated in fact, so the event log
does not currently prove *who* did anything. **Aetos in its current state must not be exposed to
an untrusted network.** `prod.json` exists so that the three-environment structure is real
rather than aspirational, and so nothing has to be reshaped when a genuine deployment target
eventually exists. It runs in SIMULATION mode like the others.

## Format

```jsonc
{
  "schema_version": "1.0.0",
  "environment": "sim",                 // must equal the file name
  "description": "…",                   // what this environment is for, in one line

  "run_mode": "simulation",             // simulation | live | hybrid

  "site": "fictional_test_range",       // document in config/sites/, no .json
  "sensor_registry": "simulated",       // document in config/sensor_registry/, no .json
  "enabled_sensor_keys": null,          // registry keys permitted here; null = all

  "event_store_path": "var/sim/events.sqlite3",   // repo-root relative unless absolute

  "queues": { … },
  "risk": { … }
}
```

### `run_mode`

`simulation` is the only mode with anything behind it. `live` and `hybrid` are accepted by the
schema so that configuration does not change shape when real adapters exist — mode selection is
adapter registry configuration, not a code path (`docs/architecture.md`, "Run modes"). Declaring
`live` today buys nothing: no real sensor integration exists, and whatever wires adapters is
responsible for refusing a mode it cannot serve. Config loading does not pretend to be that
check, because a validation rule that says "this mode is unimplemented" is a rule someone has to
remember to delete.

### `enabled_sensor_keys`

Restricts which registry keys this environment may use. `null` means every key the site's
placements reference. An environment narrows the registry; it never extends it. The registry
document itself is not parsed by the environment loader — it belongs to the sensor layer.

### `event_store_path`

Where the append-only log is written. Relative paths resolve against the repository root, never
the working directory. `null` selects an in-memory log, which has no durability at all and is
only ever appropriate for tests.

### `queues` — backpressure (ADR 0008)

Bounded queues with an explicit shed policy are what make saturation a defined, observable state
instead of unbounded memory growth followed by a crash. Capacity depends on hardware and sensor
count, so it is configuration.

| Field | Meaning |
|---|---|
| `ingestion_capacity`, `tracking_capacity`, `broadcast_capacity` | Bound on each inter-stage queue, in items |
| `shed_high_water` | Occupancy fraction at which shedding begins |
| `shed_resume_water` | Occupancy fraction at which it stops — strictly lower, or the policy chatters at exactly the load where chattering costs most |
| `sustained_shed_window_s` | Window over which shedding counts as sustained rather than momentary |
| `degraded_mode_shed_fraction` | Fraction shed within that window which activates degraded mode |

### `risk.hysteresis` — level stability (ADR 0007)

Each boundary is a Schmitt trigger: the level is entered when the score rises through
`rise_score` and left when it falls below `fall_score`. The gap is the hysteresis band.
`escalation_dwell_s` and `de_escalation_dwell_s` are the minimum hold times after a change, and
escalation is configured substantially shorter — a genuine threat must not wait out a
de-escalation timer, but one noisy update must not promote a benign object either.

Boundaries are listed lowest first with strictly increasing `rise_score`, and `fall_score` must
be strictly below `rise_score` on each. Level names are strings here rather than an enum:
`backend/risk` owns the level vocabulary and `backend/common` may not import it (ADR 0009), so
the risk engine checks these names against its own enum when it loads them.

The score itself is never smoothed. Hysteresis applies to the discrete level, and therefore to
alerting, only.

## The shipped environments

| Environment | Purpose | Notes |
|---|---|---|
| `sim` | Local development and unit runs | Modest queues; short de-escalation dwell so tests do not wait |
| `staging` | Shared soak and scenario replay | Larger queues, longer windows |
| `prod` | Placeholder — see the warning above | Not a deployment target |

All three run in SIMULATION mode against the same fictional test site. There is no real sensor
integration anywhere in this repository.
