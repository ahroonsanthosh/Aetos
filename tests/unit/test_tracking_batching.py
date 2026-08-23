"""One tracking cycle is one instant, and this is what keeps it that way.

The bug behind this module was found by connecting a real sensor, and it is the kind that only
real hardware produces: a live adapter reads on its own thread and hands over every frame that
arrived since the last poll, so a sensor faster than the pipeline tick delivers several
observations of the same object at once. `TrackManager.process` associates at most one detection
per track, so every extra observation initiated a *new* track — one target in front of the radar
became nine on the operator's screen.

Nothing in the simulator could produce it. The simulated adapter throttles itself to its
configured update rate and is polled once per cycle, so it returns at most one observation per
object per cycle by construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from backend.tracking.batching import group_by_observation
from backend.tracking.track_manager.manager import IncomingDetection

EPOCH = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def detection(sensor: str, offset_s: float, east: float = 0.0) -> IncomingDetection:
    return IncomingDetection(
        detection_id=f"{sensor}-{offset_s}-{east}",
        sensor_id=sensor,
        timestamp=EPOCH + timedelta(seconds=offset_s),
        position_enu=np.array([east, 0.0, 0.0]),
        covariance=np.eye(3),
    )


def test_nothing_produces_no_groups() -> None:
    assert group_by_observation([]) == []


def test_one_observation_per_sensor_stays_a_single_group() -> None:
    """The common case, and the one that must cost nothing: a deployment whose sensors are all
    slower than its tick behaves exactly as it did before this existed."""
    batch = [
        detection("radar", 0.0, east=10.0),
        detection("radar", 0.0, east=50.0),
        detection("camera", 0.0),
    ]

    groups = group_by_observation(batch)

    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_several_targets_in_one_frame_stay_together() -> None:
    """One frame is one instant however many targets it carries, and the tracker must see them
    all at once or they compete for each other's tracks."""
    batch = [detection("radar", 0.0, east=e) for e in (10.0, 20.0, 30.0)]

    assert len(group_by_observation(batch)) == 1


def test_successive_frames_from_one_sensor_are_split() -> None:
    """The bug. Two observations of one object in a single association pass means the second
    cannot be assigned to the track the first just updated, so it starts a new one."""
    batch = [
        detection("radar", 0.0, east=10.0),
        detection("radar", 0.1, east=11.0),
        detection("radar", 0.2, east=12.0),
    ]

    groups = group_by_observation(batch)

    assert len(groups) == 3
    assert [len(group) for group in groups] == [1, 1, 1]


def test_different_sensors_stay_together_despite_differing_timestamps() -> None:
    """The property an epsilon-based rule would have got wrong.

    Two sensors observing the same instant never carry byte-identical timestamps in production —
    each reads the clock when its own frame completes. Splitting on any timestamp difference
    would put them in separate groups and destroy cross-sensor association, which is the whole
    point of having more than one sensor.
    """
    batch = [
        detection("radar", 0.000),
        detection("camera", 0.003),
        detection("rf", 0.007),
    ]

    groups = group_by_observation(batch)

    assert len(groups) == 1
    assert {d.sensor_id for d in groups[0]} == {"radar", "camera", "rf"}


def test_a_fast_sensor_and_a_slow_one_interleave_correctly() -> None:
    """The realistic mixed case: a 10 Hz radar and a 2 Hz camera against a 0.5 s tick."""
    batch = [
        detection("radar", 0.0),
        detection("camera", 0.0),
        detection("radar", 0.1),
        detection("radar", 0.2),
    ]

    groups = group_by_observation(batch)

    assert len(groups) == 3
    assert {d.sensor_id for d in groups[0]} == {"radar", "camera"}
    assert [d.sensor_id for d in groups[1]] == ["radar"]
    assert [d.sensor_id for d in groups[2]] == ["radar"]


def test_groups_come_back_in_time_order_whatever_order_they_arrived() -> None:
    """Adapters are polled in configuration order, not in observation order, so a batch arrives
    shuffled. Feeding the tracker an earlier instant after a later one would make it predict
    backwards."""
    batch = [
        detection("radar", 0.2),
        detection("radar", 0.0),
        detection("radar", 0.1),
    ]

    groups = group_by_observation(batch)
    times = [group[0].timestamp for group in groups]

    assert times == sorted(times)


def test_every_detection_survives_the_split() -> None:
    """Grouping reorganises; it must never discard. Losing a detection here would look exactly
    like a sensor that missed one."""
    batch = [
        detection("radar", 0.0, east=1.0),
        detection("radar", 0.0, east=2.0),
        detection("radar", 0.1, east=3.0),
        detection("camera", 0.05),
    ]

    regrouped = [d for group in group_by_observation(batch) for d in group]

    assert len(regrouped) == len(batch)
    assert {d.detection_id for d in regrouped} == {d.detection_id for d in batch}
