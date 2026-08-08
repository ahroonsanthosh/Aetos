"""The ground truth a simulated sensor is pointed at, and the seam that keeps it out of reach.

`TruthSource` is the only way the simulated adapter learns where anything is. Everything the
adapter emits is a degraded observation of what it reads through this interface — never a copy
of it (ADR 0002).

Keeping truth behind an interface also keeps the adapter free of scenario mechanics. The
adapter does not know what a scenario is, how a trajectory is integrated, or that objects
appear and disappear on a schedule; it asks what is in the air at a moment and models what it
would have seen. `scenarios/world.py` provides the implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.common.geodesy import EnuPoint
from sensors.schemas.detection import ClassificationLabel


@dataclass(frozen=True, slots=True)
class TruthObject:
    """One object's exact state at one instant, in the site's local ENU frame.

    ENU rather than WGS-84 because every consumer of it — range, bearing, elevation, radial
    velocity — is metric geometry, and doing that in degrees is the mistake ADR 0004 exists to
    prevent. The adapter converts to WGS-84 once, at the point of emitting.
    """

    object_id: str
    position: EnuPoint
    velocity_east_mps: float
    velocity_north_mps: float
    velocity_up_mps: float
    label: ClassificationLabel
    """What the object *is*. A simulated sensor may only ever use this as the input to its
    confusion model, never as the label it reports."""


@runtime_checkable
class TruthSource(Protocol):
    """Whatever is in the air at a given moment.

    Queried by wall time rather than by elapsed seconds so that every sensor sees the same
    world at the same instant. Two sensors polling at different rates, or started at slightly
    different times, must not disagree about where an object was — that would manufacture
    sensor disagreement that no sensor model accounts for, and scenario G would then be
    testing the simulator's bookkeeping rather than fusion.
    """

    def objects_at(self, when: datetime) -> tuple[TruthObject, ...]:
        """Objects present at `when`. Empty before any appears and after all have gone."""
        ...
