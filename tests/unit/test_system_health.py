"""The sensor watchdog: noticing a sensor has stopped, without crying wolf about a quiet sky.

FM-010's hazard is that tracks keep being drawn from data that has stopped arriving, and
nothing says so. The difficulty is that **a sensor producing nothing may be broken, or may be
looking at an empty sky**, and one sensor watched alone cannot tell those apart.

Both failure directions are expensive, so both are tested here. Guessing "broken" fires the
indicator through every quiet minute until an operator stops reading it — the failure ADR 0015
spends its length avoiding for alerts. Guessing "empty sky" stays silent through exactly the
event the watchdog exists to catch.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.system_health.models import SensorCondition
from backend.system_health.policy import HealthPolicy
from backend.system_health.watchdog import SensorWatchdog
from sensors.adapters.interface import SensorHealth, SensorState
from sensors.schemas.detection import SensorType

START = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def events(clock: SimClock) -> SqliteEventStore:
    store = SqliteEventStore(clock)
    yield store
    store.close()


@pytest.fixture
def policy() -> HealthPolicy:
    return HealthPolicy(default_silence_s=60.0, minimum_contributing_sensors=1)


def report(
    sensor_id: str,
    *,
    state: SensorState = SensorState.OK,
    last_detection: datetime | None = START,
    emitted: int = 25,
    reason: str = "nominal",
    sensor_type: SensorType = SensorType.SIMULATED_RADAR,
) -> SensorHealth:
    return SensorHealth(
        sensor_id=sensor_id,
        sensor_type=sensor_type,
        state=state,
        last_detection_time=last_detection,
        detections_emitted=emitted,
        reason=reason,
    )


def condition(health, sensor_id: str) -> SensorCondition:
    return next(s.condition for s in health.sensors if s.sensor_id == sensor_id)


# --------------------------------------------------------------------------------------
# The ambiguity at the centre of FM-010
# --------------------------------------------------------------------------------------


def test_a_sensor_that_stops_while_others_report_is_silent(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """The case the watchdog exists to catch. One sensor produces nothing while its neighbours
    produce steadily — anomalous in a way one sensor's own silence never is."""
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("A"), report("B")])

    clock.advance(policy.default_silence_s * 2)
    health = watchdog.poll([report("A", last_detection=clock.now()), report("B")])

    assert condition(health, "A") is SensorCondition.OK
    assert condition(health, "B") is SensorCondition.SILENT


def test_a_quiet_sky_is_not_a_fault(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """Nothing anywhere is producing, so no sensor's silence says anything about that sensor.

    Reported as the fact it is, with the ambiguity stated, rather than as an accusation. A
    watchdog that flagged every quiet minute would be ignored by the time it mattered.
    """
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("A"), report("B")])

    clock.advance(policy.default_silence_s * 2)
    health = watchdog.poll([report("A"), report("B")])

    assert condition(health, "A") is SensorCondition.OK
    assert condition(health, "B") is SensorCondition.OK
    assert "empty sky" in next(s.reason for s in health.sensors if s.sensor_id == "A")


def test_a_sensor_that_has_never_reported_is_not_accused(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """A sensor with nothing yet in its coverage is indistinguishable from a blind one.

    Calling it silent would flag every short-range sensor at a site whose action is far away.
    The count of zero is on the record for an operator to read; the conclusion is not drawn.
    """
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("A"), report("B", last_detection=None, emitted=0)])

    clock.advance(policy.default_silence_s * 5)
    health = watchdog.poll(
        [report("A", last_detection=clock.now()), report("B", last_detection=None, emitted=0)]
    )

    assert condition(health, "B") is SensorCondition.OK
    assert next(s.detections_emitted for s in health.sensors if s.sensor_id == "B") == 0


def test_a_single_sensor_site_never_calls_its_only_sensor_silent(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """It has no neighbours, so it has no evidence. Degrading honestly matters more than
    appearing to check — this is the configuration the testbed radar will run in."""
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("only")])

    clock.advance(policy.default_silence_s * 10)
    health = watchdog.poll([report("only")])

    assert condition(health, "only") is SensorCondition.OK


# --------------------------------------------------------------------------------------
# Thresholds are configured, not derived from update rate
# --------------------------------------------------------------------------------------


def test_a_sensor_type_may_be_allowed_a_longer_quiet_spell(
    clock: SimClock, events: SqliteEventStore
) -> None:
    """Regression against deriving the threshold from update rate.

    A 10 Hz narrow-field EO/IR polls every 100 ms and sees the target only while it is in view.
    Measured across all nine scenarios with no fault present, its longest legitimate quiet spell
    was 81 s against the radars' 10.5 s — so a threshold derived from poll rate flagged the
    healthiest sensor on the site continuously.
    """
    policy = HealthPolicy(
        default_silence_s=30.0,
        silence_by_sensor_type={"simulated_eo_ir": 300.0},
    )
    watchdog = SensorWatchdog(policy, clock, events)
    reports = [
        report("radar"),
        report("camera", sensor_type=SensorType.SIMULATED_EO_IR),
    ]
    watchdog.poll(reports)

    clock.advance(90.0)
    health = watchdog.poll(
        [
            report("radar", last_detection=clock.now()),
            report("camera", sensor_type=SensorType.SIMULATED_EO_IR),
        ]
    )

    assert condition(health, "camera") is SensorCondition.OK


def test_an_individual_sensor_override_beats_its_type(
    clock: SimClock, events: SqliteEventStore
) -> None:
    policy = HealthPolicy(
        default_silence_s=300.0,
        silence_by_sensor_type={"simulated_radar": 300.0},
        silence_by_sensor_id={"B": 10.0},
    )
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("A"), report("B")])

    clock.advance(30.0)
    health = watchdog.poll([report("A", last_detection=clock.now()), report("B")])

    assert condition(health, "A") is SensorCondition.OK
    assert condition(health, "B") is SensorCondition.SILENT


def test_a_threshold_of_zero_is_refused() -> None:
    """It would report every sensor as silent on every cycle."""
    with pytest.raises(ValueError, match="silence threshold"):
        HealthPolicy(silence_by_sensor_id={"A": 0.0})


# --------------------------------------------------------------------------------------
# The adapter's own report is taken at its word
# --------------------------------------------------------------------------------------


def test_an_adapter_reporting_offline_is_offline(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    watchdog = SensorWatchdog(policy, clock, events)
    health = watchdog.poll(
        [report("A"), report("B", state=SensorState.OFFLINE, reason="cable pulled")]
    )

    assert condition(health, "B") is SensorCondition.OFFLINE
    assert next(s.reason for s in health.sensors if s.sensor_id == "B") == "cable pulled"


def test_a_degraded_sensor_still_counts_as_contributing(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """Reduced capability it can name is still capability. Treating it as lost would declare a
    site degraded for a sensor that is still producing."""
    watchdog = SensorWatchdog(policy, clock, events)
    health = watchdog.poll([report("A", state=SensorState.DEGRADED, reason="rain fade")])

    assert condition(health, "A") is SensorCondition.DEGRADED
    assert health.contributing_sensors == 1


def test_an_offline_sensor_does_not_accrue_silence_while_it_is_down(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """Its clock restarts when it comes back, rather than counting the time it spent
    legitimately stopped against it."""
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("A"), report("B", state=SensorState.OFFLINE, last_detection=None)])

    clock.advance(policy.default_silence_s * 3)
    watchdog.poll([report("A"), report("B", state=SensorState.OFFLINE, last_detection=None)])

    clock.advance(1.0)
    health = watchdog.poll(
        [report("A", last_detection=clock.now()), report("B", last_detection=None, emitted=0)]
    )

    assert condition(health, "B") is SensorCondition.OK


# --------------------------------------------------------------------------------------
# Degraded mode
# --------------------------------------------------------------------------------------


def test_a_site_with_nothing_contributing_is_degraded(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    watchdog = SensorWatchdog(policy, clock, events)
    health = watchdog.poll(
        [
            report("A", state=SensorState.OFFLINE),
            report("B", state=SensorState.OFFLINE),
        ]
    )

    assert health.degraded is True
    assert "incomplete" in health.reason


def test_degraded_mode_is_recorded_on_entry_and_exit(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("A", state=SensorState.OFFLINE)])
    watchdog.poll([report("A", state=SensorState.OFFLINE)])  # unchanged: no second event
    watchdog.poll([report("A")])

    recorded = [
        e.event_type
        for e in events.read_from(1)
        if e.event_type
        in (EventType.DEGRADED_MODE_ENTERED, EventType.DEGRADED_MODE_EXITED)
    ]
    assert recorded == [EventType.DEGRADED_MODE_ENTERED, EventType.DEGRADED_MODE_EXITED]


def test_a_healthy_site_is_assessed_and_not_degraded(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    watchdog = SensorWatchdog(policy, clock, events)
    health = watchdog.poll([report("A"), report("B")])

    assert health.assessed is True
    assert health.degraded is False
    assert health.contributing_sensors == 2
    assert health.reason, "a healthy posture must still say it was checked"


def test_nothing_polled_yet_is_unassessed_rather_than_healthy(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """"Nobody has looked" and "looked and found nothing wrong" must never render the same
    way (ADR 0011)."""
    watchdog = SensorWatchdog(policy, clock, events)

    assert watchdog.health.assessed is False
    assert watchdog.health.degraded is False


# --------------------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------------------


def test_only_condition_changes_are_recorded(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """A sensor reporting OK at 2 Hz would otherwise fill the log with the fact that nothing
    happened."""
    watchdog = SensorWatchdog(policy, clock, events)
    for _ in range(5):
        watchdog.poll([report("A")])
        clock.advance(0.5)

    changes = [e for e in events.read_from(1) if e.event_type is EventType.SENSOR_STATUS_CHANGED]
    assert len(changes) == 1
    assert changes[0].payload["condition"] == "ok"


def test_a_status_change_names_what_it_changed_from(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """A condition is only interpretable against what it was before."""
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("A")])
    watchdog.poll([report("A", state=SensorState.OFFLINE, reason="link lost")])

    change = [
        e for e in events.read_from(1) if e.event_type is EventType.SENSOR_STATUS_CHANGED
    ][-1]
    assert change.payload["condition"] == "offline"
    assert change.payload["previous_condition"] == "ok"
    assert change.payload["reason"] == "link lost"


def test_anomalous_sensors_are_reported_worst_first(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.poll([report("keeps-going"), report("stops"), report("dies")])

    clock.advance(policy.default_silence_s * 2)
    health = watchdog.poll(
        [
            report("keeps-going", last_detection=clock.now()),
            report("stops"),
            report("dies", state=SensorState.OFFLINE),
        ]
    )

    assert [s.sensor_id for s in health.anomalous] == ["dies", "stops"]


def test_silence_is_measured_from_first_being_seen_running(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """Measuring from process start would count a gap that happened before the sensor was ever
    polled against it."""
    watchdog = SensorWatchdog(policy, clock, events)
    clock.advance(10_000.0)
    health = watchdog.poll([report("A", last_detection=None, emitted=0)])

    silent_for = next(s.silent_for_s for s in health.sensors if s.sensor_id == "A")
    assert silent_for == pytest.approx(0.0)


def test_the_expected_interval_is_reported_as_context(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """Carried for an operator to read, and deliberately *not* used as the silence threshold —
    poll rate is not detection rate."""
    watchdog = SensorWatchdog(policy, clock, events)
    watchdog.set_expected_interval("A", 0.25)
    health = watchdog.poll([report("A")])

    assert next(s.expected_interval_s for s in health.sensors if s.sensor_id == "A") == 0.25


# --------------------------------------------------------------------------------------
# Hostile and degenerate input
# --------------------------------------------------------------------------------------


def test_remembered_state_does_not_grow_without_bound(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """Both internal maps are keyed by sensor id and nothing removed from them.

    Found by pentest: 300 cycles of changing identifiers left 307 entries behind, and nothing
    would ever have reclaimed them. Real adapters have stable ids so it is bounded in practice,
    but a deployment that rotates sensors, or a process outliving a site reconfiguration, grew
    it for as long as it ran.
    """
    watchdog = SensorWatchdog(policy, clock, events)
    for cycle in range(200):
        watchdog.poll([report("stable"), report(f"transient-{cycle}")])
        clock.advance(1.0)

    assert len(watchdog._conditions) <= 2
    assert len(watchdog._observed_since) <= 2


def test_a_detection_timestamped_in_the_future_does_not_go_negative(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """A skewed clock or a hostile adapter must not produce a negative silence."""
    watchdog = SensorWatchdog(policy, clock, events)
    health = watchdog.poll([report("A", last_detection=START + timedelta(days=365))])

    assert health.sensors[0].silent_for_s == pytest.approx(0.0)


def test_a_site_with_no_sensors_says_so_rather_than_implying_failure(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    """"0 of 0 sensors are contributing" reads as something broken. Nothing was ever attached,
    which is the expected state of an idle system and a different sentence."""
    watchdog = SensorWatchdog(policy, clock, events)
    health = watchdog.poll([])

    assert health.assessed is True
    assert "No sensors are configured" in health.reason


def test_polling_a_large_fleet_stays_within_a_cycle(
    clock: SimClock, events: SqliteEventStore, policy: HealthPolicy
) -> None:
    watchdog = SensorWatchdog(policy, clock, events)
    reports = [report(f"S{index:04d}") for index in range(2_000)]

    started = time.perf_counter()
    watchdog.poll(reports)

    assert time.perf_counter() - started < 0.25
