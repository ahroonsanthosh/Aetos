# `config/` — everything a deployment is allowed to change without a code change

## Responsibility

Configuration is data. No site, coordinate, radius or threshold appears in application code;
they live here, are validated against Pydantic models at load, and fail loudly when wrong.

| Directory | Holds | Model |
|---|---|---|
| `sites/` | Protected geography: reference point, zones, sensor placements | `backend/zones/site.py` |
| `environments/` | Run mode, which site, event store path, queue and risk tuning | `backend/common/config.py` |
| `sensor_registry/` | Simulation profiles: how a simulated sensor degrades ground truth | owned by the sensor layer |
| `hardware/` | Physically fitted sensors: link settings, mounting, measured error | owned by the sensor layer |

Each directory documents its own format in its own README.

## How loading works

`backend/common/config.py` provides the generic machinery: find the file, read it, validate it
against a model, and raise `ConfigError` naming the file and the offending field if it does not
hold up. It is parameterised by model type and knows nothing about what a zone or a sensor is —
`backend/common` may not import any other backend module (ADR 0009), so the site-specific
loader lives in `backend/zones/geometry.py` and the registry loader belongs to the sensor layer.

Paths resolve from the repository root, never the working directory, so a process started from
anywhere loads the same configuration. Set `AETOS_CONFIG_ROOT` to point at a different tree; an
explicit `ConfigPaths` argument overrides even that, which is how tests stay independent of the
ambient environment.

There is no partial load and no silent default for a required field. A misconfigured site must
stop the system at startup rather than produce a plausible-looking picture built on geometry
nobody intended.

## Simulated profiles and fitted hardware are different directories

`sensor_registry/` and `hardware/` are both keyed by `registry_key` and a site placement names
one key, so which directory the key is found in decides whether that placement is a simulation
or a device on a serial port. They are kept apart rather than merged with a type field for one
reason: a merged directory lets a typo fall back from a real sensor to a simulated profile,
and an operator watching synthetic returns believing they are measurements is the single most
dangerous state this system can reach.

## Naming rule

A document's declared identity must match its file name — `sites/fictional_test_range.json`
must declare `"site_id": "fictional_test_range"`, and `environments/sim.json` must declare
`"environment": "sim"`. A file copied, renamed and not edited is otherwise indistinguishable
from the thing it claims to be, and a site reference point is load-bearing configuration
(ADR 0004), not a label.

## Test data only

The shipped site is fictional. It describes no real installation, customer or address, and the
shipped environments run in SIMULATION mode against synthetic detections only. See
`environments/README.md` for why `prod.json` is a placeholder and not a deployment target.
