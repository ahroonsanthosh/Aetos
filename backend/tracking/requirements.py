"""What tracking requires of a sensor, derived from tracking's own tuning.

`sensors/adapters/capabilities.py` defines the floors a source is measured against but carries
no numbers, deliberately: the thresholds belong to whoever owns the constants they come from,
and that is this module. Anything written down twice stops agreeing the first time one copy is
retuned, and a sensor specification that has quietly stopped describing the tracker is worse
than none — it would certify hardware the system cannot actually use.

So the direction of knowledge is one-way. `sensors/` knows nothing about tracks; `backend/
tracking` knows what it needs from a sensor and says so here. See ADR 0017.

## What is derived, and what is policy

Most of this is arithmetic over constants that already exist. Four values are not, and they are
named as constants below with their reasoning attached: the separation two objects must be
distinguishable at, the fraction of the protected radius a position error may consume, the
design speed timing error is judged against, and the margin applied to that timing budget.

Those four are site policy wearing the clothes of a default. They are exposed as parameters so a
deployment can state its own, and they are the figures to revisit first when a site's threat
model differs from the one assumed here.
"""

from __future__ import annotations

import math

from backend.tracking.association.gnn import AssociationConfig
from backend.tracking.track_manager.manager import TrackManagerConfig
from sensors.adapters.capabilities import FloorThresholds

DEFAULT_MINIMUM_SEPARATION_M = 50.0
"""How far apart two objects the site must keep distinct may be.

Fifty metres is a loose multirotor formation. A site whose threat model is a single intruder can
accept far more; a site expecting coordinated multiples needs less, and this figure tightens
every uncertainty conclusion that follows from it.

Used only to compute discrimination range, which is reported rather than gated on: a sensor that
cannot separate close targets still tracks isolated ones perfectly well."""

DECISION_SCALE_UNCERTAINTY_FRACTION = 0.1
"""Fraction of the protected radius a one-sigma position error may consume before a zone
determination stops meaning anything.

At one tenth, an object reported inside the protected area is inside it. At one half, "inside or
outside" is close to a coin toss and the risk score built on top of it is decoration. The
specific value is a judgement; that the bound must scale with the site rather than being a fixed
number of metres is not — a 200 m compound and an 8 km aerodrome do not have the same tolerance
for the same error."""

DESIGN_SPEED_MPS = 30.0
"""Target speed the timing budget is judged against. A small multirotor at a brisk cruise.

Timing error becomes position error at this speed, and unlike measurement noise it is
systematic, so it does not average out over updates."""

TIMING_MARGIN = 2.0
"""How far below the discrimination ceiling timing-induced error is held.

Timing is one contributor among several and must not be the dominant one. A margin of two keeps
it comfortably minor at the design speed; at unity, timing error alone would consume the entire
discrimination budget and the sensor's own accuracy would stop mattering."""


def sensor_floor_thresholds(
    *,
    pipeline_tick_s: float,
    protected_radius_m: float,
    minimum_separation_m: float = DEFAULT_MINIMUM_SEPARATION_M,
    design_speed_mps: float = DESIGN_SPEED_MPS,
    track_config: TrackManagerConfig | None = None,
    association_config: AssociationConfig | None = None,
) -> FloorThresholds:
    """Thresholds a sensor must clear in *this* deployment, computed from live constants.

    `pipeline_tick_s` and `protected_radius_m` are required rather than defaulted because both
    are deployment facts this module has no business guessing — the tick belongs to whatever
    drives the pipeline, and the radius to the site document.
    """
    tracking = track_config or TrackManagerConfig()
    association = association_config or AssociationConfig()

    if pipeline_tick_s <= 0.0:
        raise ValueError(f"pipeline_tick_s must be positive, got {pipeline_tick_s}")
    if protected_radius_m <= 0.0:
        raise ValueError(f"protected_radius_m must be positive, got {protected_radius_m}")

    gate_radius_sigmas = math.sqrt(association.gate_threshold)

    # The update-rate floor comes from *tentative* track retirement, not from the coast window,
    # and reading only the friendlier of the two is the trap this line exists to avoid. A
    # confirmed track coasts on elapsed time and survives a gap of `coast_seconds`. A tentative
    # one is retired after a fixed number of missed cycles — counted in cycles rather than
    # seconds, deliberately, because what disqualifies it is a lack of corroboration rather than
    # the passage of time. So a source slower than this can never accumulate the detections that
    # confirmation requires, however long it observes and however accurate it is.
    update_period_ceiling_s = tracking.tentative_max_misses * pipeline_tick_s

    # The uncertainty at which two objects at the site's minimum separation stop being
    # separable: neither one's gate may reach the other's detection, so each gate radius has to
    # fit inside half the separation. Not a floor requirement — it is what discrimination range
    # is computed from — but it is also the natural scale for the timing budget, because timing
    # error and position error are competing for the same room.
    discrimination_sigma_ceiling_m = minimum_separation_m / (2.0 * gate_radius_sigmas)

    return FloorThresholds(
        update_period_ceiling_s=update_period_ceiling_s,
        # One update period. Beyond it the picture is always more than one update stale and
        # prediction is extrapolating from a position that has already been superseded.
        latency_ceiling_ms=update_period_ceiling_s * 1000.0,
        position_uncertainty_ceiling_m=(
            protected_radius_m * DECISION_SCALE_UNCERTAINTY_FRACTION
        ),
        timestamp_uncertainty_ceiling_ms=(
            1000.0 * discrimination_sigma_ceiling_m / (design_speed_mps * TIMING_MARGIN)
        ),
        gate_radius_sigmas=gate_radius_sigmas,
        minimum_separation_m=minimum_separation_m,
        decision_scale_m=protected_radius_m,
    )
