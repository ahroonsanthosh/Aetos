"""Global Nearest Neighbour association with Mahalanobis gating.

This module answers one question per cycle: which detection, if any, belongs to which track.

The obvious implementation — for each detection, take the closest track — is greedy nearest
neighbour, and it does not merely perform poorly at a track crossing, it fails there **by
construction**. At the moment two objects cross, each detection genuinely is closest to the
wrong track, and greedy assignment takes the bait. Scenario H would be unpassable.

So the assignment is solved globally, over Mahalanobis distances rather than Euclidean ones.
See docs/adr/0005-gnn-association.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats import chi2

from backend.tracking.filters.kalman import ConstantVelocityFilter, KalmanState

MEASUREMENT_DOF = 3
"""Degrees of freedom of the gate: a three-dimensional position measurement."""


@dataclass(frozen=True, slots=True)
class AssociationConfig:
    """Gate tuning.

    `gate_confidence` is the probability mass of the chi-squared distribution the gate
    admits. At 0.99, a detection genuinely belonging to a track is wrongly excluded roughly
    one time in a hundred — which costs one missed update on a track that will coast through
    it, and is much cheaper than the alternative failure, where a loose gate lets a track
    capture a detection belonging to its neighbour and the two identities swap.
    """

    gate_confidence: float = 0.99

    @property
    def gate_threshold(self) -> float:
        """Squared Mahalanobis distance above which a pairing is impossible, not merely
        expensive. Derived from the chi-squared quantile rather than hand-tuned, so the
        threshold means something statistical rather than being a magic number."""
        return float(chi2.ppf(self.gate_confidence, MEASUREMENT_DOF))


@dataclass(frozen=True, slots=True)
class Candidate:
    """One detection, reduced to what association needs: where and how uncertain."""

    key: str
    """Identifies the detection to the caller. Association does not care what it means."""

    position_enu: np.ndarray
    covariance: np.ndarray


@dataclass(frozen=True, slots=True)
class TrackCandidate:
    """One track's predicted state at the time of the detections being associated."""

    track_id: str
    state: KalmanState


@dataclass(frozen=True, slots=True)
class AssociationResult:
    """Who got what, and what was left over.

    The three fields are exhaustive and disjoint: every track and every detection passed in
    appears in exactly one of them. Association that quietly loses a detection is worse than
    association that makes a wrong choice, because the wrong choice is at least visible.
    """

    assignments: dict[str, str] = field(default_factory=dict)
    """track_id -> detection key."""

    unassigned_detections: list[str] = field(default_factory=list)
    """Candidates for new tracks."""

    unassigned_tracks: list[str] = field(default_factory=list)
    """Tracks that received nothing this cycle and must coast."""

    costs: dict[str, float] = field(default_factory=dict)
    """track_id -> squared Mahalanobis distance of the accepted pairing. Retained because it
    is the natural measure of how well a track is being supported, and the track manager uses
    it when deciding confidence."""


class GlobalNearestNeighbourAssociator:
    """Assigns detections to tracks by solving the whole assignment problem at once.

    Two properties do the work, and both matter:

    - **Mahalanobis distance**, which weights each axis by the filter's actual uncertainty
      along it. A coasting track has a wide predicted covariance and should accept a detection
      further away; a well-observed track should not.
    - **Global assignment**, which minimises total cost across every pairing simultaneously.
      The crossing case is defined by two locally optimal choices being jointly wrong, and
      only a global solve can prefer the jointly consistent interpretation.

    Combined with velocity in the filter state — two objects at a crossing are moving in
    different directions even where their positions coincide — this is what preserves identity
    through the crossing.
    """

    def __init__(
        self,
        kalman: ConstantVelocityFilter,
        config: AssociationConfig | None = None,
    ) -> None:
        self._kalman = kalman
        self._config = config or AssociationConfig()

    @property
    def config(self) -> AssociationConfig:
        return self._config

    def associate(
        self,
        tracks: list[TrackCandidate],
        detections: list[Candidate],
    ) -> AssociationResult:
        """Assign detections to tracks for one cycle."""
        if not tracks:
            return AssociationResult(unassigned_detections=[d.key for d in detections])
        if not detections:
            return AssociationResult(unassigned_tracks=[t.track_id for t in tracks])

        cost, gated = self._cost_matrix(tracks, detections)
        row_indices, column_indices = linear_sum_assignment(cost)

        assignments: dict[str, str] = {}
        costs: dict[str, float] = {}
        claimed_tracks: set[int] = set()
        claimed_detections: set[int] = set()

        for row, column in zip(row_indices, column_indices, strict=True):
            # The solver works on a fully populated matrix, so it will happily return pairings
            # that the gate excluded — it had to put the assignment somewhere. Those are
            # discarded here rather than being prevented, because forbidding them inside the
            # matrix (with infinities) makes the problem infeasible whenever a track has no
            # admissible detection at all, which is a completely ordinary situation.
            if not gated[row, column]:
                continue

            assignments[tracks[row].track_id] = detections[column].key
            costs[tracks[row].track_id] = float(cost[row, column])
            claimed_tracks.add(row)
            claimed_detections.add(column)

        return AssociationResult(
            assignments=assignments,
            unassigned_detections=[
                d.key for i, d in enumerate(detections) if i not in claimed_detections
            ],
            unassigned_tracks=[t.track_id for i, t in enumerate(tracks) if i not in claimed_tracks],
            costs=costs,
        )

    def _cost_matrix(
        self,
        tracks: list[TrackCandidate],
        detections: list[Candidate],
    ) -> tuple[np.ndarray, np.ndarray]:
        """Squared Mahalanobis distances, and a mask of which pairings the gate admits.

        Pairings outside the gate are given a cost far above any admissible one rather than
        infinity, so the assignment problem always has a solution; the mask is what actually
        excludes them afterwards.
        """
        threshold = self._config.gate_threshold
        excluded_cost = threshold * 1e6

        cost = np.full((len(tracks), len(detections)), excluded_cost)
        gated = np.zeros((len(tracks), len(detections)), dtype=bool)

        for i, track in enumerate(tracks):
            for j, detection in enumerate(detections):
                distance = self._kalman.mahalanobis_squared(
                    track.state, detection.position_enu, detection.covariance
                )
                if distance <= threshold:
                    cost[i, j] = distance
                    gated[i, j] = True

        return cost, gated
