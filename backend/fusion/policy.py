"""Fusion tuning. Configuration, not constants — the thresholds are site judgements.

Loaded from `config/fusion/policy.json`, strictly validated, for the same reason every other
policy in this system is: burying a deployment-specific number in code hides it from the people
best placed to correct it and makes the output unauditable by anyone asked to trust it.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from backend.common.config import ConfigError, ConfigPaths

DEFAULT_POLICY_FILE = "policy.json"


class FusionPolicy(BaseModel):
    """When two tracks are judged to be observations of one object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notes: str = ""
    """Free text for whoever maintains the file, declared rather than tolerated as an unknown
    key so that `extra="forbid"` can turn a typo into a startup error."""

    separation_m: float = Field(default=150.0, gt=0.0)
    """Slant separation below which two tracks may be the same object.

    Sized to sensor error, not to object size. Two radars observing one drone at 4 km routinely
    disagree by a hundred metres or more in cross-range, and a threshold tighter than that
    would leave the fragments this module exists to reconcile unreconciled.

    The failure modes are not symmetric, which is why this is the *permissive* half of the test
    and `velocity_mps` below is the strict half. Over-merging hides a real contact; under-merging
    shows one contact twice. Both are bad, but only the first can hide a threat, so the
    discrimination is carried by velocity rather than by tightening this."""

    velocity_mps: float = Field(default=6.0, gt=0.0)
    """Velocity difference below which two co-located tracks may be the same object.

    This is the discriminating half. Two distinct aircraft can pass within 150 m of each other
    — scenario H is built on exactly that — but not while also matching course and speed to
    within a few metres per second. Requiring both is what lets the separation threshold above
    be generous without merging a crossing pair."""

    split_separation_m: float = Field(default=320.0, gt=0.0)
    """Separation two *already fused* tracks must exceed before they are split apart again.

    The hysteresis band, and it is load-bearing rather than a refinement. Fragments of one drone
    sit near the merge threshold and drift across it constantly, so a single threshold makes the
    grouping chatter: on the cycle they merge, one track stops being presented and its alert
    clears as `track_lost`; on the cycle they separate it is presented again and raises a new
    one. Measured on `b_approaching_threat`, one drone produced 34 alerts against the unfused
    pipeline's 3, every clear a `track_lost` from a group that re-formed moments later.

    This is the same Schmitt trigger ADR 0007 applies to risk levels, for the same reason and
    against the same failure: an operator interrupted repeatedly by one object learns to stop
    reading the feed."""

    split_velocity_mps: float = Field(default=14.0, gt=0.0)
    """Velocity difference two already fused tracks must exceed before they are split apart.

    The other half of the band. Generous, because two tracks of one object disagree on velocity
    most while the object manoeuvres — which is exactly when splitting them would be worst."""

    max_object_extent_m: float = Field(default=400.0, gt=0.0)
    """Hard ceiling on how far apart the two furthest members of one object may be.

    Single-link grouping chains: A joins B, B joins C, and C ends up in A's object however far
    from A it is. That chaining is *wanted* between fragments of one drone — sensor A and sensor
    C may each agree with B while disagreeing with each other — and it is catastrophic across a
    swarm, where a line of aircraft each within the merge threshold of the next collapses into
    one enormous object. Measured on `i_swarm_saturation` before this bound existed, a single
    "object" spanned **1497 m**; the widest genuine fusion across the other eight scenarios was
    327 m.

    Under-counting is the dangerous direction — an object that vanished into another is one
    nobody is watching — so this is a hard cap rather than a preference. A candidate must sit
    within it of *every* existing member, and a merge that would breach it does not happen.

    Sized from measurement: comfortably above the widest real fusion observed, far below the
    chains it exists to cut."""

    minimum_detections: int = Field(default=2, ge=1)
    """Detections a track needs before it may be fused into another track's object.

    A brand-new track from a single return has a position estimate seeded from one measurement
    and a velocity of zero with enormous uncertainty. Its apparent agreement with a real track
    is not evidence of anything, and letting it join would attach clutter to a real object and
    inflate that object's corroboration."""

    require_both_moving: bool = Field(default=True)
    """Whether the co-moving test is skipped for two effectively stationary tracks.

    Two hovering objects 100 m apart both have near-zero velocity, so the velocity test passes
    trivially and separation alone decides — which is the one case where the discriminating
    half of the test stops discriminating. With this set, two tracks that are both below
    `stationary_speed_mps` are never fused on velocity agreement alone."""

    stationary_speed_mps: float = Field(default=1.5, gt=0.0)
    """Speed below which a track counts as stationary for the rule above."""

    def model_post_init(self, _context: object) -> None:
        if self.split_separation_m < self.separation_m:
            raise ValueError(
                f"split_separation_m ({self.split_separation_m}) is below separation_m "
                f"({self.separation_m}); tracks would separate at a distance that immediately "
                "re-merges them, which is the chatter the band exists to prevent"
            )
        if self.split_velocity_mps < self.velocity_mps:
            raise ValueError(
                f"split_velocity_mps ({self.split_velocity_mps}) is below velocity_mps "
                f"({self.velocity_mps}); tracks would separate at a difference that "
                "immediately re-merges them"
            )


def load_fusion_policy(
    paths: ConfigPaths | None = None, filename: str = DEFAULT_POLICY_FILE
) -> FusionPolicy:
    """Read the fusion policy from `config/fusion/`."""
    resolved = paths or ConfigPaths.default()
    file = resolved.config_root / "fusion" / filename

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{file}: fusion policy not found") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{file}: invalid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{file}: could not be read: {exc}") from exc

    try:
        return FusionPolicy.model_validate(document)
    except Exception as exc:  # pydantic ValidationError, reported with the file that caused it
        raise ConfigError(f"{file}: invalid fusion policy: {exc}") from exc
