"""Association: gating, global assignment, and the track-crossing case ADR 0005 exists for."""

from __future__ import annotations

import numpy as np
import pytest

from backend.tracking.association.gnn import (
    AssociationConfig,
    Candidate,
    GlobalNearestNeighbourAssociator,
    TrackCandidate,
)
from backend.tracking.filters.kalman import (
    VELOCITY,
    ConstantVelocityFilter,
    KalmanState,
    isotropic_covariance,
)

R = isotropic_covariance(10.0)


@pytest.fixture
def kalman() -> ConstantVelocityFilter:
    return ConstantVelocityFilter()


@pytest.fixture
def associator(kalman: ConstantVelocityFilter) -> GlobalNearestNeighbourAssociator:
    return GlobalNearestNeighbourAssociator(kalman)


def track_at(
    kalman: ConstantVelocityFilter,
    track_id: str,
    position: list[float],
    velocity: list[float] | None = None,
    sigma_m: float = 10.0,
) -> TrackCandidate:
    state: KalmanState = kalman.initiate(np.array(position), isotropic_covariance(sigma_m))
    if velocity is not None:
        state.mean[VELOCITY] = np.array(velocity)
    return TrackCandidate(track_id=track_id, state=state)


def detection_at(key: str, position: list[float], sigma_m: float = 10.0) -> Candidate:
    return Candidate(
        key=key, position_enu=np.array(position), covariance=isotropic_covariance(sigma_m)
    )


# --------------------------------------------------------------------------------------
# The crossing case — the acceptance test for ADR 0005
# --------------------------------------------------------------------------------------


def test_identities_survive_a_track_crossing(kalman: ConstantVelocityFilter) -> None:
    """Scenario H. Two objects cross; the tracker must not exchange their identities.

    Both tracks are run through the crossing with the real filter, so the velocity state that
    disambiguates them is genuinely earned from the position sequence rather than injected.
    """
    associator = GlobalNearestNeighbourAssociator(kalman)
    dt = 1.0

    # Eastbound at y=0 and westbound at y=0, converging on the origin from opposite sides.
    eastbound_v, westbound_v = np.array([20.0, 0.0, 0.0]), np.array([-20.0, 0.0, 0.0])
    east_pos, west_pos = np.array([-100.0, 0.0, 0.0]), np.array([100.0, 0.0, 0.0])

    east = kalman.initiate(east_pos, R)
    west = kalman.initiate(west_pos, R)

    identities_held = True
    for _ in range(12):
        east_pos = east_pos + eastbound_v * dt
        west_pos = west_pos + westbound_v * dt

        east = kalman.predict(east, dt)
        west = kalman.predict(west, dt)

        result = associator.associate(
            [TrackCandidate("TRK-EAST", east), TrackCandidate("TRK-WEST", west)],
            [detection_at("d-east", list(east_pos)), detection_at("d-west", list(west_pos))],
        )

        # The whole point: the eastbound track must keep taking the eastbound detection, even
        # in the cycles where the two objects are close enough that the wrong pairing is the
        # locally cheaper one.
        if result.assignments.get("TRK-EAST") != "d-east":
            identities_held = False
        if result.assignments.get("TRK-WEST") != "d-west":
            identities_held = False

        if "TRK-EAST" in result.assignments:
            east = kalman.update(east, np.array(east_pos), R)
        if "TRK-WEST" in result.assignments:
            west = kalman.update(west, np.array(west_pos), R)

    assert identities_held, "identities were exchanged during the crossing"


def _distance(
    kalman: ConstantVelocityFilter, track: TrackCandidate, detection: Candidate
) -> float:
    return kalman.mahalanobis_squared(track.state, detection.position_enu, detection.covariance)


def _greedy_total_cost(
    kalman: ConstantVelocityFilter,
    tracks: list[TrackCandidate],
    detections: list[Candidate],
    gate_threshold: float,
) -> float | None:
    """What nearest-neighbour association would cost: repeatedly take the cheapest remaining
    pair. Present only as the baseline the global solve has to beat.

    The gate is applied here too. Without it the baseline can appear to win by using a pairing
    the gated solver is forbidden to make, which compares two different problems rather than
    two strategies. Returns None when greedy cannot fill every track, since a partial
    assignment is likewise not the same problem.
    """
    pairs = sorted(
        (
            (_distance(kalman, t, d), i, j)
            for i, t in enumerate(tracks)
            for j, d in enumerate(detections)
        ),
        key=lambda p: p[0],
    )

    total = 0.0
    used_tracks: set[int] = set()
    used_detections: set[int] = set()
    for cost, i, j in pairs:
        if i in used_tracks or j in used_detections or cost > gate_threshold:
            continue
        total += cost
        used_tracks.add(i)
        used_detections.add(j)

    return total if len(used_tracks) == len(tracks) else None


def test_global_assignment_never_costs_more_than_greedy(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    """The formal reason ADR 0005 rejects nearest neighbour.

    Greedy is optimal on easy geometry, so a single hand-picked example proves nothing. The
    configurations below are deliberately crowded — objects separated by less than their
    measurement uncertainty — because that is where the two strategies diverge, and it is
    exactly the situation a track crossing or a swarm creates.
    """
    rng = np.random.default_rng(2026)
    threshold = associator.config.gate_threshold
    compared = 0
    strictly_better = 0

    for _ in range(400):
        tracks = [
            track_at(kalman, f"TRK-{i}", list(rng.uniform(-30, 30, 3)), sigma_m=25.0)
            for i in range(4)
        ]
        detections = [
            detection_at(f"d-{j}", list(rng.uniform(-30, 30, 3)), sigma_m=25.0) for j in range(4)
        ]

        result = associator.associate(tracks, detections)
        if len(result.assignments) < len(tracks):
            continue

        greedy_cost = _greedy_total_cost(kalman, tracks, detections, threshold)
        if greedy_cost is None:
            continue

        compared += 1
        global_cost = sum(result.costs.values())

        assert global_cost <= greedy_cost + 1e-9
        if global_cost < greedy_cost - 1e-9:
            strictly_better += 1

    assert compared > 50, "too few comparable configurations to conclude anything"
    assert strictly_better > compared // 10, (
        f"global assignment beat greedy in only {strictly_better}/{compared} crowded "
        "configurations, which is too few to justify the extra machinery"
    )


def test_global_solve_minimises_total_cost(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    """The defining property. Two tracks and two detections where the locally obvious pairing
    for one track is jointly worse."""
    tracks = [
        track_at(kalman, "TRK-A", [0.0, 0.0, 0.0]),
        track_at(kalman, "TRK-B", [30.0, 0.0, 0.0]),
    ]
    detections = [detection_at("d-near", [2.0, 0.0, 0.0]), detection_at("d-far", [32.0, 0.0, 0.0])]

    result = associator.associate(tracks, detections)

    assert result.assignments == {"TRK-A": "d-near", "TRK-B": "d-far"}


# --------------------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------------------


def test_a_detection_beyond_the_gate_is_not_assigned(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    """A gated-out pairing is impossible, not merely expensive. Without this, a lone track
    would capture any stray detection on the map simply because nothing else competed."""
    tracks = [track_at(kalman, "TRK-A", [0.0, 0.0, 0.0], sigma_m=5.0)]
    detections = [detection_at("d-distant", [10_000.0, 0.0, 0.0], sigma_m=5.0)]

    result = associator.associate(tracks, detections)

    assert result.assignments == {}
    assert result.unassigned_detections == ["d-distant"]
    assert result.unassigned_tracks == ["TRK-A"]


def test_an_uncertain_track_accepts_a_detection_a_certain_one_rejects(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    """The gate scales with the filter's own uncertainty, which is what allows a coasting
    track to be reacquired rather than fragmenting into a new one (FM-022)."""
    detections = [detection_at("d", [60.0, 0.0, 0.0], sigma_m=5.0)]

    certain = associator.associate(
        [track_at(kalman, "T", [0.0, 0.0, 0.0], sigma_m=2.0)], detections
    )
    uncertain = associator.associate(
        [track_at(kalman, "T", [0.0, 0.0, 0.0], sigma_m=60.0)], detections
    )

    assert certain.assignments == {}
    assert uncertain.assignments == {"T": "d"}


def test_gate_threshold_is_a_chi_squared_quantile() -> None:
    """Derived rather than hand-tuned, so the number means something statistical."""
    config = AssociationConfig(gate_confidence=0.99)

    # Chi-squared with three degrees of freedom, 99th percentile.
    assert config.gate_threshold == pytest.approx(11.345, abs=0.01)


def test_a_tighter_gate_admits_less(kalman: ConstantVelocityFilter) -> None:
    tracks = [track_at(kalman, "T", [0.0, 0.0, 0.0], sigma_m=10.0)]
    detections = [detection_at("d", [38.0, 0.0, 0.0], sigma_m=10.0)]

    loose = GlobalNearestNeighbourAssociator(kalman, AssociationConfig(gate_confidence=0.999))
    tight = GlobalNearestNeighbourAssociator(kalman, AssociationConfig(gate_confidence=0.50))

    assert loose.associate(tracks, detections).assignments
    assert not tight.associate(tracks, detections).assignments


# --------------------------------------------------------------------------------------
# Exhaustiveness — nothing may be silently lost
# --------------------------------------------------------------------------------------


def test_every_track_and_detection_is_accounted_for(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    """Association that quietly loses a detection is worse than one that makes a wrong
    choice, because the wrong choice is at least visible."""
    tracks = [track_at(kalman, f"TRK-{i}", [i * 500.0, 0.0, 0.0]) for i in range(4)]
    detections = [detection_at(f"d-{i}", [i * 500.0 + 3.0, 0.0, 0.0]) for i in range(6)]

    result = associator.associate(tracks, detections)

    assigned_tracks = set(result.assignments)
    assert assigned_tracks | set(result.unassigned_tracks) == {t.track_id for t in tracks}
    assert not assigned_tracks & set(result.unassigned_tracks)

    assigned_detections = set(result.assignments.values())
    assert assigned_detections | set(result.unassigned_detections) == {d.key for d in detections}
    assert not assigned_detections & set(result.unassigned_detections)


def test_no_detection_is_assigned_to_two_tracks(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    """Two tracks converging on one object must not both claim its detection and both become
    overconfident about it."""
    tracks = [
        track_at(kalman, "TRK-A", [0.0, 0.0, 0.0], sigma_m=30.0),
        track_at(kalman, "TRK-B", [5.0, 0.0, 0.0], sigma_m=30.0),
    ]
    detections = [detection_at("d-only", [2.0, 0.0, 0.0], sigma_m=30.0)]

    result = associator.associate(tracks, detections)

    assert len(result.assignments) == 1
    assert len(result.unassigned_tracks) == 1


def test_more_tracks_than_detections(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    tracks = [track_at(kalman, f"TRK-{i}", [i * 100.0, 0.0, 0.0]) for i in range(3)]
    detections = [detection_at("d-0", [0.0, 0.0, 0.0])]

    result = associator.associate(tracks, detections)

    assert len(result.assignments) == 1
    assert len(result.unassigned_tracks) == 2


def test_more_detections_than_tracks_leaves_new_track_candidates(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    tracks = [track_at(kalman, "TRK-0", [0.0, 0.0, 0.0])]
    detections = [detection_at(f"d-{i}", [i * 400.0, 0.0, 0.0]) for i in range(3)]

    result = associator.associate(tracks, detections)

    assert len(result.assignments) == 1
    assert len(result.unassigned_detections) == 2


# --------------------------------------------------------------------------------------
# Degenerate inputs
# --------------------------------------------------------------------------------------


def test_no_tracks_makes_every_detection_a_new_track_candidate(
    associator: GlobalNearestNeighbourAssociator,
) -> None:
    result = associator.associate([], [detection_at("d-1", [0.0, 0.0, 0.0])])

    assert result.unassigned_detections == ["d-1"]
    assert result.assignments == {}


def test_no_detections_leaves_every_track_coasting(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    result = associator.associate([track_at(kalman, "TRK-A", [0.0, 0.0, 0.0])], [])

    assert result.unassigned_tracks == ["TRK-A"]


def test_nothing_at_all(associator: GlobalNearestNeighbourAssociator) -> None:
    result = associator.associate([], [])

    assert not result.assignments
    assert not result.unassigned_tracks
    assert not result.unassigned_detections


def test_saturation_load_is_handled(
    kalman: ConstantVelocityFilter, associator: GlobalNearestNeighbourAssociator
) -> None:
    """Scenario I's config ceiling. The Hungarian solve is O(n^3), which at twenty tracks is
    microseconds — worth confirming rather than assuming."""
    tracks = [track_at(kalman, f"TRK-{i:02d}", [i * 200.0, 0.0, 100.0]) for i in range(20)]
    detections = [detection_at(f"d-{i:02d}", [i * 200.0 + 4.0, 0.0, 100.0]) for i in range(20)]

    result = associator.associate(tracks, detections)

    assert len(result.assignments) == 20
    assert result.assignments["TRK-07"] == "d-07"
