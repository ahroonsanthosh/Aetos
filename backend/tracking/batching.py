"""Splitting a cycle's detections into the instants they were actually observed at.

`TrackManager.process` is one tracking cycle, and a cycle is **one instant**: it predicts every
track forward to now, then runs a single association pass that assigns at most one detection to
each track. That contract is stated in its own docstring — "every track is compared against
detections at the same instant" — and everything about the association gate depends on it.

A batch that spans several observation times from the same sensor breaks it, and breaks it in a
way that looks like a swarm. The second detection of one object in a single batch cannot be
associated to the track the first one just updated, so it initiates a new track. The third
initiates another. One drone crossing a car park becomes nine.

## Why this only appeared with real hardware

The simulated adapter throttles itself to its configured update rate and is polled once per
pipeline cycle, so it returns at most one observation per object per cycle by construction.
Nothing in three years of scenarios could produce the situation.

A live adapter cannot work that way. It reads from a port on its own thread and hands over
whatever arrived since the last poll — which, for a sensor faster than the pipeline tick, is
several frames. A 10 Hz radar against a 0.5 s tick delivers five, and every one of them after
the first was being read as a new object.

The fix is not to make the adapter throttle. Discarding real observations to fit the consumer's
cadence would lose data the tracker could have used, and would hide the sensor's real update
rate from everything downstream. The fix is to feed the tracker the instants it was designed
for.

## The grouping rule

A group holds **at most one observation time per sensor**. Detections from one frame share a
timestamp exactly and stay together; successive frames from one sensor land in successive
groups; and detections from *different* sensors stay in the same group even though their
timestamps differ by microseconds, which is what preserves cross-sensor association within a
cycle.

No epsilon, no tolerance to tune. The rule is structural rather than numerical, which is why it
cannot drift out of agreement with whatever update rates a deployment happens to have.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.tracking.track_manager.manager import IncomingDetection


def group_by_observation(
    detections: Sequence[IncomingDetection],
) -> list[list[IncomingDetection]]:
    """Split a cycle's detections into time-ordered groups, one instant per sensor in each.

    Returns a single group for the common case — one observation time per sensor — so a
    deployment whose sensors are slower than its tick pays nothing for this.
    """
    if not detections:
        return []

    ordered = sorted(detections, key=lambda detection: detection.timestamp)

    groups: list[list[IncomingDetection]] = []
    current: list[IncomingDetection] = []
    seen: dict[str, object] = {}

    for detection in ordered:
        previous = seen.get(detection.sensor_id)
        if previous is not None and previous != detection.timestamp:
            # This sensor has already contributed an observation at a different instant, so the
            # current group is complete. Sorting by timestamp first means the new one is always
            # later, never a reordering.
            groups.append(current)
            current = []
            seen = {}

        seen[detection.sensor_id] = detection.timestamp
        current.append(detection)

    groups.append(current)
    return groups
