"""Alert promotion and lifecycle.

The failure these tests exist to prevent is not a wrong severity. It is an alert feed an
operator stops reading — either because it flaps, or because it says nothing is wrong when
something is. Both are tested here directly rather than inferred from the policy values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.alerting.models import AlertState, ClearReason
from backend.alerting.policy import AlertPolicy
from backend.alerting.service import ALERT_ID_KEY, AlertService
from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.tracking.models import (
    EnuState,
    RiskLevel,
    Track,
    TrackClassification,
    TrackKinematics,
    TrackMetadata,
    TrackPosition,
    TrackState,
    TrackThreat,
)

EPOCH = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def events() -> SqliteEventStore:
    store = SqliteEventStore(SimClock(EPOCH))
    yield store
    store.close()


@pytest.fixture
def policy() -> AlertPolicy:
    return AlertPolicy(
        raise_at=RiskLevel.HIGH, clear_at_or_below=RiskLevel.MEDIUM, clear_dwell_s=20.0
    )


@pytest.fixture
def service(policy: AlertPolicy, events: SqliteEventStore) -> AlertService:
    return AlertService(policy, events)


def track(
    track_id: str = "TRK-00001",
    *,
    level: RiskLevel = RiskLevel.HIGH,
    score: int = 80,
    assessed: bool = True,
) -> Track:
    """A track carrying exactly the threat fields alerting reads.

    Built from the real `Track` model rather than a stub, so a change to what risk writes back
    breaks these tests instead of letting them keep passing against a shape nothing produces.
    """
    return Track(
        track_id=track_id,
        state=TrackState.CONFIRMED,
        enu=EnuState(east_m=-1200.0, north_m=0.0, up_m=120.0, velocity_east_mps=25.0),
        position=TrackPosition(latitude=51.8971, longitude=-8.4697, altitude_m=120.0),
        kinematics=TrackKinematics(speed_mps=25.0, heading_deg=90.0, vertical_rate_mps=0.0),
        classification=TrackClassification(),
        threat=TrackThreat(
            risk_score=score,
            risk_level=level,
            confidence=0.9,
            reasons=["Closing on the site at 25 m/s."],
            codes=["closing_velocity"],
            assessed=assessed,
        ),
        tracking=TrackMetadata(first_seen=EPOCH, last_seen=EPOCH, detection_count=12),
        updated_at=EPOCH,
    )


def alert_events(events: SqliteEventStore, event_type: EventType) -> list:
    return [e for e in events.read_from(1) if e.event_type is event_type]


# --------------------------------------------------------------------------------------
# Raising
# --------------------------------------------------------------------------------------


def test_a_high_track_raises_one_alert(service: AlertService, events: SqliteEventStore) -> None:
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)

    assert len(service.active) == 1
    assert service.active[0].state is AlertState.ACTIVE
    assert len(alert_events(events, EventType.ALERT_CREATED)) == 1


def test_a_medium_track_raises_nothing(service: AlertService) -> None:
    """MEDIUM is visible on the track list already. Interrupting for it is what teaches an
    operator to stop reading the feed."""
    service.review_cycle([track(level=RiskLevel.MEDIUM, score=50)], EPOCH)

    assert service.active == []


def test_a_track_holding_high_raises_only_once(
    service: AlertService, events: SqliteEventStore
) -> None:
    """The difference between an alert and a log line. One incursion is one alert, however
    many cycles it spans."""
    now = EPOCH
    for _ in range(10):
        service.review_cycle([track(level=RiskLevel.HIGH)], now)
        now += timedelta(seconds=1)

    assert len(service.active) == 1
    assert len(alert_events(events, EventType.ALERT_CREATED)) == 1


def test_an_unassessed_track_raises_nothing(service: AlertService) -> None:
    """Absence of an assessment is not an assessment of absence — but it is also not grounds
    to alert. A track risk has not reached yet must not be promoted on a default level."""
    service.review_cycle([track(level=RiskLevel.NONE, score=0, assessed=False)], EPOCH)

    assert service.active == []


# --------------------------------------------------------------------------------------
# Clearing, and the dwell that stops it flapping
# --------------------------------------------------------------------------------------


def test_a_brief_dip_does_not_clear_the_alert(
    service: AlertService, events: SqliteEventStore
) -> None:
    """The flap case. A score dipping for one cycle as the filter re-converges must not clear
    an alert and then raise a second one seconds later — the operator would read two separate
    incursions where there was one.

    Asserted on the alert's *identity*, not on the count. A cleared-then-re-raised alert also
    leaves exactly one open, so counting cannot tell the two apart — which is the whole
    distinction under test.
    """
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    original = service.active[0].alert_id

    service.review_cycle([track(level=RiskLevel.MEDIUM, score=55)], EPOCH + timedelta(seconds=2))
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH + timedelta(seconds=4))

    assert [a.alert_id for a in service.active] == [original]
    assert service.active[0].state is AlertState.ACTIVE
    assert alert_events(events, EventType.ALERT_CLEARED) == []


def test_a_sustained_drop_clears_the_alert(service: AlertService, events: SqliteEventStore) -> None:
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    service.review_cycle([track(level=RiskLevel.LOW, score=20)], EPOCH + timedelta(seconds=1))
    service.review_cycle([track(level=RiskLevel.LOW, score=20)], EPOCH + timedelta(seconds=30))

    assert service.active == []
    cleared = alert_events(events, EventType.ALERT_CLEARED)
    assert len(cleared) == 1
    assert cleared[0].payload["reason"] == str(ClearReason.RISK_SUBSIDED)


def test_the_dwell_is_continuous_not_cumulative(
    service: AlertService, events: SqliteEventStore
) -> None:
    """Twenty seconds *below* the threshold, not twenty seconds of dips adding up. A track
    oscillating around the boundary has not subsided.

    Again asserted on identity and on the absence of a clear event. Counting open alerts is
    blind here: if the dwell wrongly accumulated, the alert would clear on one dip and the
    next HIGH cycle would raise a fresh one, leaving the count at 1 and the operator looking
    at a feed that silently restarted the incident.
    """
    now = EPOCH
    service.review_cycle([track(level=RiskLevel.HIGH)], now)
    original = service.active[0].alert_id

    for _ in range(6):
        now += timedelta(seconds=8)
        service.review_cycle([track(level=RiskLevel.LOW, score=20)], now)
        now += timedelta(seconds=8)
        service.review_cycle([track(level=RiskLevel.HIGH)], now)

    assert [a.alert_id for a in service.active] == [original]
    assert alert_events(events, EventType.ALERT_CLEARED) == []
    assert len(alert_events(events, EventType.ALERT_CREATED)) == 1


def test_a_lost_track_clears_with_its_own_reason(
    service: AlertService, events: SqliteEventStore
) -> None:
    """'It turned away' and 'we stopped being able to see it' must not render identically.
    An aircraft that flew out of coverage has not been dealt with."""
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    service.review_cycle([], EPOCH + timedelta(seconds=1))

    assert service.active == []
    cleared = alert_events(events, EventType.ALERT_CLEARED)
    assert cleared[0].payload["reason"] == str(ClearReason.TRACK_LOST)


# --------------------------------------------------------------------------------------
# Acknowledgement
# --------------------------------------------------------------------------------------


def test_acknowledging_does_not_clear(service: AlertService) -> None:
    """The distinction the module exists for. Acknowledgement says a human saw it, not that
    the aircraft went away."""
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    alert_id = service.active[0].alert_id

    acknowledged = service.acknowledge(alert_id, "op-1", EPOCH + timedelta(seconds=1))

    assert acknowledged is not None
    assert acknowledged.state is AlertState.ACKNOWLEDGED
    assert len(service.active) == 1, "an acknowledged alert is still open"


def test_acknowledging_an_unknown_alert_reports_nothing(service: AlertService) -> None:
    assert service.acknowledge("ALT-99999", "op-1", EPOCH) is None


def test_escalation_withdraws_an_acknowledgement(
    service: AlertService, events: SqliteEventStore
) -> None:
    """The case that would otherwise display as handled while getting worse. A HIGH someone
    glanced at must not become a CRITICAL nobody has looked at, still showing as acknowledged.
    """
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    alert_id = service.active[0].alert_id
    service.acknowledge(alert_id, "op-1", EPOCH + timedelta(seconds=1))

    service.review_cycle([track(level=RiskLevel.CRITICAL, score=95)], EPOCH + timedelta(seconds=2))

    alert = service.active[0]
    assert alert.state is AlertState.ACTIVE
    assert alert.acknowledged_at is None
    assert alert.peak_level is RiskLevel.CRITICAL

    escalated = alert_events(events, EventType.ALERT_ESCALATED)
    assert len(escalated) == 1
    assert escalated[0].payload["acknowledgement_withdrawn"] is True


def test_peak_level_does_not_fall_with_the_current_level(service: AlertService) -> None:
    """An alert that touched CRITICAL is not retrospectively a HIGH one."""
    service.review_cycle([track(level=RiskLevel.CRITICAL, score=95)], EPOCH)
    service.review_cycle([track(level=RiskLevel.HIGH, score=70)], EPOCH + timedelta(seconds=1))

    alert = service.active[0]
    assert alert.level is RiskLevel.HIGH
    assert alert.peak_level is RiskLevel.CRITICAL


# --------------------------------------------------------------------------------------
# Durability — the log is the authority (ADR 0006)
# --------------------------------------------------------------------------------------


def test_open_alerts_survive_a_restart(policy: AlertPolicy, events: SqliteEventStore) -> None:
    """An alert that vanished because the process bounced is indistinguishable, to the
    operator, from one that was dealt with."""
    first = AlertService(policy, events)
    first.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    first.acknowledge(first.active[0].alert_id, "op-1", EPOCH + timedelta(seconds=1))
    expected = first.active[0]

    restarted = AlertService(policy, events)
    assert restarted.rebuild() == 1

    recovered = restarted.active[0]
    assert recovered.alert_id == expected.alert_id
    assert recovered.track_id == expected.track_id
    assert recovered.state is AlertState.ACKNOWLEDGED
    assert recovered.acknowledged_by == "op-1"


def test_cleared_alerts_do_not_come_back_on_rebuild(
    policy: AlertPolicy, events: SqliteEventStore
) -> None:
    first = AlertService(policy, events)
    first.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    first.review_cycle([], EPOCH + timedelta(seconds=1))

    restarted = AlertService(policy, events)

    assert restarted.rebuild() == 0
    assert restarted.active == []


def test_identifiers_are_not_reused_after_a_restart(
    policy: AlertPolicy, events: SqliteEventStore
) -> None:
    """A second ALT-00001 would make two distinct alerts indistinguishable in the record, and
    an acknowledgement of one appear to acknowledge the other."""
    first = AlertService(policy, events)
    first.review_cycle([track("TRK-00001", level=RiskLevel.HIGH)], EPOCH)
    original = first.active[0].alert_id

    restarted = AlertService(policy, events)
    restarted.rebuild()
    restarted.review_cycle(
        [
            track("TRK-00001", level=RiskLevel.HIGH),
            track("TRK-00002", level=RiskLevel.HIGH),
        ],
        EPOCH + timedelta(seconds=1),
    )

    ids = {alert.alert_id for alert in restarted.active}
    assert len(ids) == 2
    assert original in ids


def test_every_alert_carries_the_reason_codes_risk_gave_it(service: AlertService) -> None:
    """An alert that cannot say why it fired leaves an operator with obey or dismiss, and the
    second is what they will learn to do (spec §10)."""
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)

    alert = service.active[0]
    assert alert.codes == ["closing_velocity"]
    assert alert.reasons == ["Closing on the site at 25 m/s."]


# --------------------------------------------------------------------------------------
# Policy validation
# --------------------------------------------------------------------------------------


def test_a_clear_threshold_at_the_raise_threshold_is_rejected() -> None:
    """Without a gap the alert raises and clears on alternating cycles — the exact failure
    the mechanism exists to prevent."""
    with pytest.raises(ValueError, match="not below raise_at"):
        AlertPolicy(raise_at=RiskLevel.HIGH, clear_at_or_below=RiskLevel.HIGH)


def test_raising_at_none_is_rejected() -> None:
    with pytest.raises(ValueError, match="every track"):
        AlertPolicy(raise_at=RiskLevel.NONE, clear_at_or_below=RiskLevel.NONE)


def test_the_shipped_policy_leaves_a_hysteresis_band() -> None:
    """Guards the config file itself, not just the model — the shipped values are what runs."""
    from backend.alerting.policy import load_alert_policy

    shipped = load_alert_policy()

    assert shipped.clear_at_or_below.severity < shipped.raise_at.severity
    assert shipped.clear_dwell_s > 0


def test_clearing_records_the_alert_it_cleared(
    service: AlertService, events: SqliteEventStore
) -> None:
    """Every event after the first names the alert by its own ID, not by the creating event's
    — an alert outlives the event that raised it."""
    service.review_cycle([track(level=RiskLevel.HIGH)], EPOCH)
    raised = service.active[0].alert_id
    service.review_cycle([], EPOCH + timedelta(seconds=1))

    cleared = alert_events(events, EventType.ALERT_CLEARED)[0]
    assert cleared.payload[ALERT_ID_KEY] == raised


def test_a_retired_duplicates_alert_closes_as_a_duplicate_not_as_lost(
    events: SqliteEventStore,
) -> None:
    """ADR 0021. "Track lost" tells an operator the object may still be out there unwatched.
    When the tracker retired a second track on an object another track still follows, that is
    false — the incident continues under the survivor — so the alert says what happened and
    names the survivor. It does so even on a site that keeps lost-track alerts open, because a
    duplicate's alert is a second alert for one incident, not an object that left coverage."""
    service = AlertService(
        AlertPolicy(
            raise_at=RiskLevel.HIGH,
            clear_at_or_below=RiskLevel.MEDIUM,
            clear_on_track_lost=False,
        ),
        events,
    )
    now = EPOCH
    service.review_cycle([track("TRK-00001"), track("TRK-00045")], now)
    assert len(service.active) == 2

    service.review_cycle([track("TRK-00001")], now, {"TRK-00045": "TRK-00001"})

    assert [a.track_id for a in service.active] == ["TRK-00001"]
    (cleared,) = [e for e in events.read_from(1) if e.event_type is EventType.ALERT_CLEARED]
    assert cleared.payload["reason"] == "duplicate_track"
    assert cleared.payload["duplicate_of"] == "TRK-00001"


def test_an_ordinary_lost_track_is_still_reported_as_lost(
    service: AlertService, events: SqliteEventStore
) -> None:
    """The duplicate path must not swallow the honest one."""
    service.review_cycle([track("TRK-00001")], EPOCH)
    service.review_cycle([], EPOCH, {})

    (cleared,) = [e for e in events.read_from(1) if e.event_type is EventType.ALERT_CLEARED]
    assert cleared.payload["reason"] == "track_lost"


def test_a_retired_duplicates_alert_moves_to_a_survivor_that_has_none(
    service: AlertService, events: SqliteEventStore
) -> None:
    """ADR 0021. Measured before this existed: a HIGH alert on a retired duplicate closed while
    the survivor — the same aircraft, held at MEDIUM by its own history — had no alert, and the
    operator's alert on a real, continuing threat simply ended. The alert follows the object
    instead: same incident, same identifier, same acknowledgement, now on the survivor."""
    service.review_cycle([track("TRK-00045")], EPOCH)
    (raised,) = service.active
    service.acknowledge(raised.alert_id, "alice", EPOCH)

    later = EPOCH + timedelta(seconds=1)
    service.review_cycle(
        [track("TRK-00001", level=RiskLevel.MEDIUM, score=55)], later, {"TRK-00045": "TRK-00001"}
    )

    (moved,) = service.active
    assert moved.alert_id == raised.alert_id
    assert moved.track_id == "TRK-00001"
    assert moved.state is AlertState.ACKNOWLEDGED
    assert alert_events(events, EventType.ALERT_CLEARED) == []
    (transfer,) = alert_events(events, EventType.ALERT_TRANSFERRED)
    assert transfer.payload["from_track_id"] == "TRK-00045"
    assert alert_events(events, EventType.ALERT_CREATED)[1:] == [], "a second alert was raised"

    rebuilt = AlertService(service._policy, events)
    rebuilt.rebuild()
    assert [(a.alert_id, a.track_id, a.state) for a in rebuilt.active] == [
        (raised.alert_id, "TRK-00001", AlertState.ACKNOWLEDGED)
    ]
    rebuilt.review_cycle([track("TRK-00001", level=RiskLevel.MEDIUM, score=55)], later)
    assert len(rebuilt.active) == 1, "the rebuilt service still maps the alert to the old track"
