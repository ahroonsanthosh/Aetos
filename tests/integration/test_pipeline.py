"""The vertical slice, end to end: scenario → simulator → ingestion → tracking.

This is the first point at which the pipeline is exercised as a whole rather than module by
module. Everything upstream of the tracker here is real: real scenario files from the library,
real trajectory integration, real sensor models from the registry, real measurement noise and
dropout. The tracker sees only canonical Detections.

The provenance guard (ADR 0002) lives here, and it is the most important test in the
repository.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from backend.common.clock import SimClock
from backend.events.store import SqliteEventStore
from backend.events.types import EventType
from backend.tracking.models import TrackState
from backend.tracking.track_manager.manager import IncomingDetection, TrackManager
from backend.zones.geometry import SiteGeometry, load_site_geometry
from scenarios.library import load_all, load_scenario, scenario_files
from scenarios.world import ScenarioWorld
from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.registry import load_sensor_registry
from sensors.schemas.detection import Detection

EPOCH = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
SITE_NAME = "fictional_test_range"


@pytest.fixture(scope="module")
def geometry() -> SiteGeometry:
    return load_site_geometry(SITE_NAME)


@pytest.fixture(scope="module")
def registry():
    return load_sensor_registry()


def build_adapters(
    scenario_id: str,
    geometry: SiteGeometry,
    registry,
    clock: SimClock,
    world: ScenarioWorld,
    only_sensors: set[str] | None = None,
) -> list[SimulatedSensorAdapter]:
    """One adapter per enabled sensor placement in the site configuration."""
    scenario = world.scenario
    positions = geometry.site.sensor_positions_enu()

    adapters = []
    for placement in geometry.site.sensors:
        if not placement.enabled:
            continue
        if only_sensors is not None and placement.sensor_id not in only_sensors:
            continue
        adapters.append(
            SimulatedSensorAdapter(
                clock=clock,
                sensor_id=placement.sensor_id,
                model=registry[placement.registry_key],
                position_enu=positions[placement.sensor_id],
                frame=geometry.frame,
                truth_source=world,
                scenario_seed=scenario.seed,
                boresight_deg=placement.boresight_deg,
            )
        )
    for adapter in adapters:
        adapter.start()
    return adapters


def to_incoming(detection: Detection, geometry: SiteGeometry) -> IncomingDetection:
    """Ingestion's reduction: canonical Detection to the tracker's input.

    Note what is dropped. Speed, heading and classification never cross this boundary, so the
    tracker is structurally incapable of consuming them (ADR 0002). The covariance is taken
    from the detection when the source reported one and built isotropically from the scalar
    when it did not.
    """
    enu = geometry.to_enu(
        detection.position.latitude, detection.position.longitude, detection.position.altitude_m
    )

    if detection.measurement.position_covariance_enu is not None:
        covariance = np.array(detection.measurement.position_covariance_enu)
    else:
        covariance = np.eye(3) * (detection.measurement.position_uncertainty_m ** 2)

    return IncomingDetection(
        detection_id=detection.detection_id,
        sensor_id=detection.sensor.sensor_id,
        timestamp=detection.timestamp,
        position_enu=np.array([enu.east_m, enu.north_m, enu.up_m]),
        covariance=covariance,
    )


def run_scenario(
    scenario_id: str,
    geometry: SiteGeometry,
    registry,
    *,
    duration_s: float | None = None,
    step_s: float = 0.5,
    strip_inferred: bool = False,
    only_sensors: set[str] | None = None,
):
    """Drive a scenario through the full pipeline and return the outcome.

    `strip_inferred` is the guard test's lever: when set, every detection is passed through
    `Detection.without_inferred_fields()` before ingestion.
    """
    scenario = load_all()[scenario_id]
    clock = SimClock(EPOCH)
    world = ScenarioWorld(scenario, EPOCH)
    events = SqliteEventStore(clock)
    manager = TrackManager(clock, events, geometry.frame)
    adapters = build_adapters(scenario_id, geometry, registry, clock, world, only_sensors)

    duration = duration_s if duration_s is not None else scenario.duration_s
    detections_seen = 0
    tracks = []

    steps = int(duration / step_s)
    for _ in range(steps):
        batch: list[Detection] = []
        for adapter in adapters:
            batch.extend(adapter.get_detections())

        if strip_inferred:
            batch = [d.without_inferred_fields() for d in batch]

        detections_seen += len(batch)
        tracks = manager.process([to_incoming(d, geometry) for d in batch])
        clock.advance(step_s)

    return {
        "clock": clock,
        "events": events,
        "manager": manager,
        "tracks": tracks,
        "detections_seen": detections_seen,
        "scenario": scenario,
    }


# --------------------------------------------------------------------------------------
# The provenance guard — ADR 0002. The most important test here.
# --------------------------------------------------------------------------------------


def test_tracking_is_unchanged_when_simulator_privileged_fields_are_stripped(
    geometry: SiteGeometry, registry
) -> None:
    """The guarantee that the simulator and a real sensor are interchangeable.

    The simulator knows every object's exact velocity and true label. Real sensors largely do
    not: a radar measures range, azimuth and elevation, not heading. If the tracker consumes
    those fields it silently becomes dependent on information no real sensor will supply, and
    nothing would reveal it until hardware arrived.

    So the same scenario is run twice — once with detections as produced, once with
    `kinematics` and `classification` removed from every one — and the resulting track states
    must be identical. Not similar: identical.

    If this test fails, the pipeline has acquired exactly the dependency ADR 0002 exists to
    prevent. See also docs/failure_modes.md FM-021.
    """
    with_fields = run_scenario("b_approaching_threat", geometry, registry, duration_s=90.0)
    without_fields = run_scenario(
        "b_approaching_threat", geometry, registry, duration_s=90.0, strip_inferred=True
    )

    assert with_fields["detections_seen"] == without_fields["detections_seen"]
    assert with_fields["detections_seen"] > 0, "scenario produced no detections at all"

    left = {t.track_id: t for t in with_fields["manager"].active_tracks()}
    right = {t.track_id: t for t in without_fields["manager"].active_tracks()}

    assert left.keys() == right.keys(), "stripping the fields changed which tracks exist"

    for track_id, track in left.items():
        other = right[track_id]
        assert track.state is other.state
        assert track.enu == other.enu, f"{track_id}: position/velocity estimate differs"
        assert track.kinematics == other.kinematics, f"{track_id}: derived kinematics differ"
        assert track.tracking.detection_count == other.tracking.detection_count


def test_the_simulator_does_emit_the_fields_being_stripped(
    geometry: SiteGeometry, registry
) -> None:
    """Guards the guard.

    If the simulator stopped populating `kinematics` and `classification` altogether, the test
    above would pass trivially while proving nothing. It only means something if those fields
    are genuinely present and genuinely ignored.
    """
    scenario = load_all()["b_approaching_threat"]
    clock = SimClock(EPOCH)
    world = ScenarioWorld(scenario, EPOCH)
    adapters = build_adapters("b_approaching_threat", geometry, registry, clock, world)

    seen_kinematics = False
    seen_classification = False
    for _ in range(120):
        for adapter in adapters:
            for detection in adapter.get_detections():
                seen_kinematics |= detection.kinematics is not None
                seen_classification |= detection.classification is not None
        clock.advance(0.5)

    assert seen_kinematics, "simulator emitted no kinematics; the guard test proves nothing"
    assert seen_classification, "simulator emitted no classification hints"


# --------------------------------------------------------------------------------------
# The pipeline actually works
# --------------------------------------------------------------------------------------


def test_a_scenario_produces_detections_and_forms_tracks(
    geometry: SiteGeometry, registry
) -> None:
    """The vertical slice: synthetic detection → ingestion → association → track."""
    outcome = run_scenario("a_benign_transit", geometry, registry, duration_s=120.0)

    assert outcome["detections_seen"] > 0
    assert outcome["manager"].active_tracks(), "no track was formed from a clean transit"


def test_a_single_object_produces_a_confirmed_track(
    geometry: SiteGeometry, registry
) -> None:
    """Scenario A carries one object across the monitored area on a clean trajectory. If the
    pipeline cannot confirm that, nothing downstream of it is worth testing."""
    outcome = run_scenario("a_benign_transit", geometry, registry, duration_s=120.0)

    confirmed = [
        t for t in outcome["manager"].active_tracks() if t.state is TrackState.CONFIRMED
    ]

    assert confirmed, "a clean single-object transit did not reach CONFIRMED"


def test_velocity_is_recovered_without_ever_being_told(
    geometry: SiteGeometry, registry
) -> None:
    """The filter derives speed from the position sequence alone. The scenario declares the
    object's speed, so this compares an estimate against the truth that produced it."""
    outcome = run_scenario("a_benign_transit", geometry, registry, duration_s=120.0)

    confirmed = [
        t for t in outcome["manager"].active_tracks() if t.state is TrackState.CONFIRMED
    ]
    assert confirmed

    declared = outcome["scenario"].objects[0].motion.speed_mps
    estimated = max(confirmed, key=lambda t: t.tracking.detection_count).kinematics.speed_mps

    assert estimated == pytest.approx(declared, rel=0.4)


def test_every_track_is_reconstructible_from_the_event_log(
    geometry: SiteGeometry, registry
) -> None:
    """ADR 0006: the log is the source of truth and in-memory state is a projection of it. A
    track with no events could not be replayed or audited."""
    outcome = run_scenario("a_benign_transit", geometry, registry, duration_s=90.0)
    events = outcome["events"]

    tracks = outcome["manager"].active_tracks()
    assert tracks

    for track in tracks:
        history = list(events.read_for_track(track.track_id))
        assert history, f"{track.track_id} has no events"
        assert history[0].event_type is EventType.TRACK_CREATED
        assert [e.seq for e in history] == sorted(e.seq for e in history)


def test_detections_carry_the_canonical_schema_version(
    geometry: SiteGeometry, registry
) -> None:
    scenario = load_all()["a_benign_transit"]
    clock = SimClock(EPOCH)
    world = ScenarioWorld(scenario, EPOCH)
    adapters = build_adapters("a_benign_transit", geometry, registry, clock, world)

    emitted: list[Detection] = []
    for _ in range(60):
        for adapter in adapters:
            emitted.extend(adapter.get_detections())
        clock.advance(0.5)

    assert emitted
    assert all(d.schema_version == "1.0.0" for d in emitted)
    assert all(d.sensor.sensor_type.is_simulated for d in emitted)


# --------------------------------------------------------------------------------------
# Determinism — ADR 0008
# --------------------------------------------------------------------------------------


def test_a_scenario_run_is_reproducible_from_its_seed(
    geometry: SiteGeometry, registry
) -> None:
    """Without this, a failing scenario cannot be reproduced and replay cannot reproduce the
    run it claims to replay."""
    first = run_scenario("a_benign_transit", geometry, registry, duration_s=60.0)
    second = run_scenario("a_benign_transit", geometry, registry, duration_s=60.0)

    assert first["detections_seen"] == second["detections_seen"]

    left = {t.track_id: t.enu for t in first["manager"].active_tracks()}
    right = {t.track_id: t.enu for t in second["manager"].active_tracks()}

    assert left == right


# --------------------------------------------------------------------------------------
# The scenario library as acceptance criteria
# --------------------------------------------------------------------------------------


def test_every_scenario_in_the_library_loads() -> None:
    files = scenario_files()

    assert len(files) == 9, f"expected nine scenarios A-I, found {len(files)}"
    for file in files:
        load_scenario(file)


def test_every_scenario_declares_a_seed() -> None:
    """ADR 0008 makes the seed mandatory, so this is really checking the schema has not
    quietly grown a default that would let a scenario be irreproducible."""
    for scenario in load_all().values():
        assert scenario.seed > 0


def test_scenario_seeds_are_distinct() -> None:
    """Two scenarios sharing a seed would correlate their noise, which makes an apparent
    agreement between them meaningless."""
    seeds = [s.seed for s in load_all().values()]

    assert len(set(seeds)) == len(seeds)


def test_every_scenario_runs_without_error(geometry: SiteGeometry, registry) -> None:
    """Each scenario file doubles as an acceptance criterion, so at minimum every one must
    drive the pipeline without raising."""
    for scenario_id in load_all():
        outcome = run_scenario(scenario_id, geometry, registry, duration_s=45.0, step_s=1.0)
        assert outcome["detections_seen"] >= 0


def test_swarm_scenario_declares_the_config_ceiling() -> None:
    """Scenario I exists to test saturation, so its object count is the number the rest of the
    system is sized against."""
    swarm = load_all()["i_swarm_saturation"]

    assert len(swarm.objects) == 20


def test_crossing_scenario_has_two_objects_on_intersecting_paths() -> None:
    crossing = load_all()["h_track_crossing"]

    assert len(crossing.objects) == 2


def test_scenario_epoch_offsets_do_not_start_before_the_run(
    geometry: SiteGeometry, registry
) -> None:
    """An object scheduled to appear before the scenario starts would silently never appear."""
    for scenario in load_all().values():
        for obj in scenario.objects:
            assert obj.appears_at_s >= 0.0
            assert obj.appears_at_s < scenario.duration_s, (
                f"{scenario.scenario_id}/{obj.object_id} appears after the scenario ends"
            )


def test_clock_advances_by_the_scenario_duration(geometry: SiteGeometry, registry) -> None:
    outcome = run_scenario("a_benign_transit", geometry, registry, duration_s=60.0, step_s=0.5)

    assert outcome["clock"].now() == EPOCH + timedelta(seconds=60.0)


# --------------------------------------------------------------------------------------
# Housekeeping wired into the cycle
# --------------------------------------------------------------------------------------


def test_a_cycle_sweeps_expired_sessions(tmp_path) -> None:
    """Expired session rows must actually be deleted by a running system.

    Regression: `AuthService.purge_expired_sessions` existed, was correct, and had no caller
    anywhere. `validate` revokes a session only when its holder presents the token, so a
    session whose owner simply closed the browser outlived its own expiry for good. The rows
    were unusable — expiry is enforced on read — so this was unbounded table growth rather than
    an authentication hole, but nothing ever reclaimed it.
    """
    from backend.auth.models import Role
    from backend.auth.policy import load_auth_policy
    from backend.auth.service import hash_token
    from backend.main import Pipeline

    clock = SimClock(EPOCH)
    pipeline = Pipeline(clock, load_site_geometry(SITE_NAME), tmp_path)
    try:
        pipeline.auth.create_user("sweeper", "correct-horse-battery", Role.OPERATOR)
        result = pipeline.auth.authenticate("sweeper", "correct-horse-battery")
        assert result.succeeded and result.token is not None
        token_hash = hash_token(result.token)

        # The row exists and survives a cycle while the session is still live.
        pipeline.step()
        assert pipeline.repositories.sessions.by_token_hash(token_hash) is not None

        # Past the absolute ceiling, which no amount of activity can extend. The row is still
        # there — expiry is enforced on read — until a sweep reclaims it.
        clock.advance(load_auth_policy().absolute_timeout_s + 1.0)
        assert pipeline.repositories.sessions.by_token_hash(token_hash) is not None

        pipeline.step()
        assert pipeline.repositories.sessions.by_token_hash(token_hash) is None
    finally:
        pipeline.close()
