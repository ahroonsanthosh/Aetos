"""The watchdog: what it is allowed to conclude, and what it must refuse to conclude.

The failure these tests exist to prevent is FM-010 in both of its directions. A dead sensor
must not render as a quiet sky — that is the obvious half. The half that is easier to get wrong
is the other one: a quiet sky must not be reported as a fault, because a watchdog that cries
wolf on every calm night is one operators learn to ignore, and an ignored watchdog is worse
than none at all.

So these tests assert on the *vocabulary* as much as the logic. `QUIET` and `OFFLINE` must stay
different words, and neither may collapse into the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.common.clock import SimClock
from backend.common.config import ConfigError
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.system_health.models import Posture, SensorVerdict
from backend.system_health.policy import SystemHealthPolicy, load_system_health_policy
from backend.system_health.watchdog import Watchdog
from sensors.adapters.interface import SensorHealth, SensorState
from sensors.schemas.detection import SensorType

EPOCH = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


@pytest.fixture
def store(clock: SimClock) -> SqliteEventStore:
    return SqliteEventStore(clock)


@pytest.fixture
def policy() -> SystemHealthPolicy:
    return SystemHealthPolicy(
        default_silence_threshold_s=30.0,
        startup_grace_s=10.0,
        stall_threshold_s=5.0,
    )


@pytest.fixture
def watchdog(clock: SimClock, policy: SystemHealthPolicy, store: SqliteEventStore) -> Watchdog:
    return Watchdog(clock, policy, store)


def health(
    state: SensorState = SensorState.OK,
    *,
    sensor_id: str = "radar-1",
    last_detection_time: datetime | None = None,
    reason: str = "Running.",
    sensor_type: SensorType = SensorType.RADAR,
) -> SensorHealth:
    return SensorHealth(
        sensor_id=sensor_id,
        sensor_type=sensor_type,
        state=state,
        last_detection_time=last_detection_time,
        reason=reason,
    )


def verdicts(assessment) -> dict[str, SensorVerdict]:
    return {s.sensor_id: s.verdict for s in assessment.sensors}


def events(store: SqliteEventStore, event_type: EventType) -> list:
    return [e for e in store.read_from(1) if e.event_type is event_type]


# --- silence is a fact, not a fault -------------------------------------------------------


def test_silence_beyond_the_window_is_quiet_and_not_a_fault(
    watchdog: Watchdog, clock: SimClock
) -> None:
    """The central claim of FM-010, stated as a test.

    A radar in contact and saying nothing for longer than the site expects is QUIET. It is not
    OFFLINE and it is not DEGRADED, because nothing observed here distinguishes a broken sensor
    from an empty sky — and the system must not invent that distinction.
    """
    watchdog.assess([health(last_detection_time=clock.now())])
    clock.advance(31.0)

    assessment = watchdog.assess([health(last_detection_time=EPOCH)])

    assert verdicts(assessment)["radar-1"] is SensorVerdict.QUIET
    assert assessment.posture is Posture.NOMINAL, "a quiet sky is not degraded mode"


def test_a_quiet_sensor_reports_the_evidence_not_only_the_word(
    watchdog: Watchdog, clock: SimClock
) -> None:
    """An operator asking *why* must not have to read the policy file to find out."""
    clock.advance(31.0)
    assessment = watchdog.assess([health(last_detection_time=EPOCH)])

    (sensor,) = assessment.sensors
    assert sensor.seconds_since_detection == pytest.approx(31.0)
    assert sensor.silence_threshold_s == 30.0
    assert "not evidence of failure" in sensor.reason or "not a fault claim" in sensor.reason


def test_recent_detections_keep_a_sensor_ok(watchdog: Watchdog, clock: SimClock) -> None:
    clock.advance(100.0)
    assessment = watchdog.assess(
        [health(last_detection_time=clock.now() - timedelta(seconds=5))]
    )
    assert verdicts(assessment)["radar-1"] is SensorVerdict.OK


def test_startup_grace_suppresses_the_first_window(watchdog: Watchdog, clock: SimClock) -> None:
    """Without this every restart reports every sensor quiet, and nobody reads the row again."""
    assessment = watchdog.assess([health(last_detection_time=None)])
    assert verdicts(assessment)["radar-1"] is SensorVerdict.OK

    clock.advance(11.0)
    assessment = watchdog.assess([health(last_detection_time=None)])
    assert verdicts(assessment)["radar-1"] is SensorVerdict.QUIET


# --- loss of contact is definite ----------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        (SensorState.OFFLINE, SensorVerdict.OFFLINE),
        (SensorState.DEGRADED, SensorVerdict.DEGRADED),
        (SensorState.NOT_STARTED, SensorVerdict.NOT_STARTED),
    ],
)
def test_the_adapters_own_account_is_never_overridden_upwards(
    watchdog: Watchdog, clock: SimClock, state: SensorState, verdict: SensorVerdict
) -> None:
    """A source that says it is broken is broken, however recently it produced data.

    The data only gets a say in the OK case, which is the only one where the adapter's account
    and the evidence can disagree.
    """
    assessment = watchdog.assess([health(state, last_detection_time=clock.now())])
    assert verdicts(assessment)["radar-1"] is verdict


def test_one_dead_sensor_degrades_the_posture_and_is_named(
    watchdog: Watchdog, clock: SimClock
) -> None:
    """Not an average. Four radars and one dead cable is degraded, and the summary says which."""
    assessment = watchdog.assess(
        [
            health(sensor_id="radar-1", last_detection_time=clock.now()),
            health(sensor_id="radar-2", last_detection_time=clock.now()),
            health(SensorState.OFFLINE, sensor_id="radar-3", reason="port closed"),
        ]
    )
    assert assessment.posture is Posture.DEGRADED
    assert "radar-3" in assessment.summary


def test_no_source_observing_is_blind_not_merely_degraded(
    watchdog: Watchdog, clock: SimClock
) -> None:
    assessment = watchdog.assess(
        [
            health(SensorState.OFFLINE, sensor_id="radar-1", reason="port closed"),
            health(SensorState.OFFLINE, sensor_id="radar-2", reason="port closed"),
        ]
    )
    assert assessment.posture is Posture.BLIND


def test_nothing_attached_is_blind(watchdog: Watchdog) -> None:
    """An empty display with no sensor behind it means nothing, and must not read as healthy."""
    assert watchdog.assess([]).posture is Posture.BLIND


def test_excluding_every_source_by_policy_is_blind_not_nominal(
    clock: SimClock, store: SqliteEventStore
) -> None:
    """A watchdog watching nothing must not report a green light nothing computes."""
    watchdog = Watchdog(
        clock,
        SystemHealthPolicy(assess_simulated_sensors=False, stall_threshold_s=5.0),
        store,
    )
    assessment = watchdog.assess(
        [health(sensor_id="sim-1", sensor_type=SensorType.SIMULATED_RADAR)]
    )
    assert assessment.sensors == []
    assert assessment.posture is Posture.BLIND


# --- the self-test: a stalled pipeline ----------------------------------------------------


def test_a_pipeline_that_stops_stepping_reports_stalled(
    watchdog: Watchdog, clock: SimClock
) -> None:
    """The one check that cannot be defeated by the thing it checks having stopped.

    `assess` is never called again; only the clock moves. Everything inside the pipeline would
    go on reporting the last healthy answer, which is why the age is evaluated at read time.
    """
    watchdog.assess([health(last_detection_time=clock.now())])
    assert watchdog.report().posture is Posture.NOMINAL

    clock.advance(6.0)
    stalled = watchdog.report()
    assert stalled.posture is Posture.STALLED
    assert "no longer current" in stalled.summary
    assert watchdog.latest.posture is Posture.NOMINAL, "the recorded pass is not rewritten"


def test_a_watchdog_that_has_never_run_is_stalled_not_nominal(watchdog: Watchdog) -> None:
    assert watchdog.report().posture is Posture.STALLED


def test_report_appends_nothing(watchdog: Watchdog, clock: SimClock, store) -> None:
    """The request path calls this. It must not be able to move the system's state."""
    watchdog.assess([health(last_detection_time=clock.now())])
    before = store.latest_seq()
    clock.advance(600.0)
    watchdog.report()
    watchdog.report()
    assert store.latest_seq() == before


# --- what reaches the log -----------------------------------------------------------------


def test_sensor_status_is_recorded_on_transition_only(
    watchdog: Watchdog, clock: SimClock, store: SqliteEventStore
) -> None:
    """A log recording a healthy radar twice a second buries the moment it stopped."""
    for _ in range(5):
        watchdog.assess([health(last_detection_time=clock.now())])
        clock.advance(1.0)

    assert len(events(store, EventType.SENSOR_STATUS_CHANGED)) == 1

    watchdog.assess([health(SensorState.OFFLINE, reason="port closed")])
    changes = events(store, EventType.SENSOR_STATUS_CHANGED)
    assert len(changes) == 2
    assert changes[-1].payload["previous"] == "ok"
    assert changes[-1].payload["verdict"] == "offline"


def test_entering_and_leaving_degraded_mode_are_both_recorded(
    watchdog: Watchdog, clock: SimClock, store: SqliteEventStore
) -> None:
    """A recovery nobody logged is indistinguishable from one that never happened."""
    watchdog.assess([health(last_detection_time=clock.now())])
    watchdog.assess([health(SensorState.DEGRADED, reason="half rate")])
    watchdog.assess([health(last_detection_time=clock.now())])

    assert len(events(store, EventType.DEGRADED_MODE_ENTERED)) == 1
    assert len(events(store, EventType.DEGRADED_MODE_EXITED)) == 1


def test_a_detached_sensor_can_transition_again_when_it_returns(
    watchdog: Watchdog, clock: SimClock, store: SqliteEventStore
) -> None:
    """State held for a sensor no longer attached would silently suppress its next report."""
    watchdog.assess([health(last_detection_time=clock.now())])
    watchdog.assess([])
    watchdog.assess([health(last_detection_time=clock.now())])

    changes = events(store, EventType.SENSOR_STATUS_CHANGED)
    assert len(changes) == 2
    assert changes[-1].payload["previous"] is None


# --- policy -------------------------------------------------------------------------------


def test_shipped_policy_loads_and_covers_every_sensor_type() -> None:
    """A sensor type with no threshold falls back to a default that suits none of them."""
    loaded = load_system_health_policy()
    for sensor_type in SensorType:
        assert sensor_type in loaded.silence_threshold_s, sensor_type


def test_a_stall_threshold_below_the_tick_is_refused() -> None:
    """It would declare the system stalled on every healthy cycle, training the operator to
    ignore the one alarm nothing else can raise."""
    with pytest.raises(ConfigError, match="stall_threshold_s"):
        SystemHealthPolicy(stall_threshold_s=0.4).validate_against_tick(0.5)

    SystemHealthPolicy(stall_threshold_s=10.0).validate_against_tick(0.5)


def test_thresholds_are_per_sensor_type() -> None:
    """The whole reason this is policy and not a constant: an order of magnitude between an
    imaging sensor and a radio-frequency one."""
    loaded = load_system_health_policy()
    assert loaded.threshold_for(SensorType.RF) > loaded.threshold_for(SensorType.EO_IR)
