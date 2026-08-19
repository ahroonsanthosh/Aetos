"""What fusion produces: objects, assembled from the tracks that observe them.

The vocabulary matters and is used consistently from here on. A **track** is one continuous
observation maintained by the tracker. An **object** is a thing in the air. Before this module
existed the two were the same word, which is precisely why an operator watching three sensors
counted three drones (FM-023).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.tracking.models import Track


@dataclass(frozen=True, slots=True)
class FusedObject:
    """One physical object and every track believed to be an observation of it."""

    object_id: str
    primary: Track
    """The track presented for this object. Chosen, never synthesised — see
    `FusionService._choose_primary`."""

    members: tuple[Track, ...]
    """Every member, primary included, ordered primary first then by track id."""

    corroborating_sensors: tuple[str, ...]
    """Union of contributing sensors across all members, sorted.

    The honest corroboration figure for the object. A single member's own sensor list describes
    one observation of it and understates what the site as a whole has seen."""

    @property
    def is_fused(self) -> bool:
        """Whether more than one track was reconciled into this object."""
        return len(self.members) > 1

    @property
    def member_ids(self) -> tuple[str, ...]:
        return tuple(member.track_id for member in self.members)

    @property
    def supporting(self) -> tuple[Track, ...]:
        """The members that are not the primary."""
        return tuple(m for m in self.members if m.track_id != self.primary.track_id)
