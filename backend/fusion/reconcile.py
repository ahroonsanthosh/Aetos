"""Reconciling several sensors' observations of one object into one measurement.

## The problem this exists for

Three sensors watch one drone. Each produces a detection every cycle, so the tracker receives
three. Global nearest neighbour assigns at most one detection per track — correctly, because two
detections of *different* objects must never collapse into one track (ADR 0005) — so two of the
three are unassigned, and unassigned detections start new tracks. Every stage behaves exactly as
designed and the operator sees three contacts where there is one (FM-023).

The fix cannot live in association. Loosening the gate until it absorbs the surplus would let a
track capture a detection belonging to its neighbour, which is an identity swap: two real objects
merging into one, the failure scenario H exists to catch. It is strictly worse than the problem,
because an over-count is visible and an under-count is not.

So the reconciliation happens *before* tracking, on detections, where the question is not "which
track does this belong to" but "are these two the same object".

## Why measurement fusion rather than track fusion

The alternative architecture runs three trackers and merges their tracks afterwards. It is the
right choice when the sensors are far apart and separately intelligent — each producing its own
confident picture — and the wrong one here.

Fusing measurements first means the filter sees one better-conditioned observation instead of
three competing ones, so the estimate benefits from all three sensors rather than being the best
of them. It also keeps exactly one component in the system holding track state, which is what
lets the log rebuild into what the live system holds (ADR 0006). Merging tracks afterwards would
require reconciling three filters' disagreeing covariances into one, and deciding which of three
identifiers survives — a question with no good answer that the operator would see as tracks
changing id.

## What decides that two detections are one object

The statistical gate decides it: squared Mahalanobis distance between the two positions, under
their combined covariance, below a chi-squared threshold. This is the same criterion, at the same
confidence, that the tracker uses to decide a detection belongs to a track — so fusion cannot
merge a pair the tracker would have insisted on keeping apart.

Two bounds constrain that gate, and which one does the work matters.

The gate scales with reported uncertainty, so a sensor claiming an enormous covariance produces a
gate wide enough to swallow genuinely separate contacts — fusion at its most aggressive exactly
where the measurements are least trustworthy. The bound that fixes this constrains the
**measurement**, not the separation: a detection whose own uncertainty exceeds
`max_fusable_uncertainty_m` is not fused with anything, because it is not evidence about *which*
object it is.

A flat distance ceiling was tried first and is the wrong instrument. To catch a pathological
covariance it has to sit below the legitimate spread of good sensors — and this site's own radars
report sigma around 45-75 m, so two true observations of one object are routinely 200-300 m
apart. Set tight enough to be useful, it split the very tracks fusion exists to join. It survives
as `max_pair_separation_m`, set generously, to bound the arithmetic rather than to make the
decision.

And one structural rule: **two detections from the same sensor are never fused.** A sensor
reports each object once per scan, so two returns from one sensor in one cycle are two objects —
or a duplicate, which ingestion already suppressed by `detection_id` (FM-013). Fusing them would
mean concluding a sensor saw one object twice, which the input cannot mean.

That rule is also what makes the clustering safe. Single-linkage grouping is otherwise prone to
chaining — A near B, B near C, so A, B and C are one cluster even though A and C are far apart —
and the sensor constraint bounds every cluster at one detection per sensor, which is the true
shape of the thing being reconstructed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from backend.fusion.models import FusedDetection
from backend.fusion.policy import FusionPolicy
from backend.tracking.track_manager.manager import IncomingDetection

MULTI_SENSOR_MARKER = "fused"
"""`sensor_id` given to a measurement built from more than one sensor.

Not one of the contributing sensors, deliberately. Naming one of them would attribute the
measurement to a device that only supplied part of it, and the track's contributing-sensor set
would then under-report its own support.
"""


@dataclass(frozen=True, slots=True)
class FusionResult:
    """One cycle's reconciliation, and enough about it to explain the outcome."""

    detections: list[FusedDetection]
    """One entry per distinct object, in stable order. The tracker's input."""

    objects_from_detections: tuple[int, int]
    """(detections in, objects out). Equal when nothing was reconciled."""

    @property
    def reconciled(self) -> int:
        """How many detections were absorbed into another. Zero when fusion changed nothing."""
        detections_in, objects_out = self.objects_from_detections
        return detections_in - objects_out


class DetectionReconciler:
    """Groups a cycle's detections by object and fuses each group into one measurement."""

    def __init__(self, policy: FusionPolicy | None = None) -> None:
        self._policy = policy or FusionPolicy()

    @property
    def policy(self) -> FusionPolicy:
        return self._policy

    def reconcile(self, detections: list[IncomingDetection]) -> FusionResult:
        """Reduce a cycle's detections to one measurement per distinct object.

        Order-stable: the output follows the order the objects were first seen in the input, so
        an unchanged input produces an unchanged output and a replayed scenario reconciles
        identically (ADR 0008 §1).
        """
        if not self._policy.enabled or len(detections) < 2:
            return FusionResult(
                detections=[_as_fused(detection) for detection in detections],
                objects_from_detections=(len(detections), len(detections)),
            )

        clusters = self._cluster(detections)
        fused = [self._fuse(cluster) for cluster in clusters]
        return FusionResult(
            detections=fused, objects_from_detections=(len(detections), len(fused))
        )

    def _cluster(self, detections: list[IncomingDetection]) -> list[list[IncomingDetection]]:
        """Single-linkage grouping, bounded to one detection per sensor per group.

        Greedy and order-stable rather than globally optimal. A globally optimal partition would
        need a cost function balancing "merged two objects" against "split one object" — and
        those errors are not commensurable: an over-count is visible to the operator and an
        under-count is not, so a formulation that traded them off symmetrically would be wrong
        even where it was optimal.
        """
        clusters: list[list[IncomingDetection]] = []

        for detection in detections:
            admitting = [cluster for cluster in clusters if self._admits(cluster, detection)]

            # Ambiguous: this detection is a plausible observation of more than one object, so
            # there is no evidence here for choosing between them. Fusing it into either would
            # be a guess presented to the operator as a reconciliation.
            #
            # This is what makes a crossing safe. As two contacts converge, their detections
            # start gating against both clusters, and fusion backs off to leaving them alone
            # rather than merging two real objects into one — an over-count for the few cycles
            # of closest approach, which the operator can see, instead of an under-count, which
            # they cannot. Separating objects that close is the tracker's job, not fusion's: it
            # holds velocity state and can tell two crossing contacts apart (ADR 0005), and
            # fusion, working on one cycle of positions, cannot.
            if len(admitting) == 1:
                admitting[0].append(detection)
            else:
                clusters.append([detection])

        return clusters

    def _admits(self, cluster: list[IncomingDetection], candidate: IncomingDetection) -> bool:
        """Whether `candidate` is an observation of the object `cluster` describes.

        Every existing member must gate against the candidate, not merely the nearest one. That
        is what stops a cluster from chaining across a line of detections, each close to the
        last and none close to the first.
        """
        if self._policy.require_distinct_sensors and any(
            member.sensor_id == candidate.sensor_id for member in cluster
        ):
            return False

        return all(self._same_object(member, candidate) for member in cluster)

    def _is_fusable(self, detection: IncomingDetection) -> bool:
        """Whether this measurement is precise enough to reconcile against anything at all.

        A detection whose own uncertainty exceeds the bound is not evidence about *which* object
        it is, and fusing on it would let one vague measurement absorb a contact it cannot
        actually locate. Left alone instead, which over-counts — the visible error.
        """
        sigma = float(np.sqrt(np.max(np.diag(detection.covariance))))
        return sigma <= self._policy.max_fusable_uncertainty_m

    def _same_object(self, left: IncomingDetection, right: IncomingDetection) -> bool:
        if not (self._is_fusable(left) and self._is_fusable(right)):
            return False

        offset = left.position_enu - right.position_enu

        if float(np.linalg.norm(offset)) > self._policy.max_pair_separation_m:
            return False

        combined = left.covariance + right.covariance
        try:
            distance_squared = float(offset @ np.linalg.solve(combined, offset))
        except np.linalg.LinAlgError:
            # A singular combined covariance means at least one sensor reported a degenerate
            # uncertainty. Refusing to fuse is the safe direction: the cost is an over-count,
            # which the operator can see, rather than a merge, which they cannot.
            return False

        return distance_squared <= self._policy.gate_threshold

    def _fuse(self, cluster: list[IncomingDetection]) -> FusedDetection:
        """Combine a cluster into one measurement by inverse-covariance weighting.

        The standard linear-Gaussian combination: information (inverse covariance) adds, and the
        fused mean is the information-weighted average. A confident sensor therefore pulls the
        result towards its own reading in proportion to how confident it actually is, rather
        than every sensor getting an equal vote regardless of quality.

        The fused covariance is smaller than any input's, which is the point — three independent
        observations of one object genuinely do locate it better than any one of them. That
        claim is only sound because the inputs are independent measurements of the same thing,
        which is exactly what the clustering above establishes and what the same-sensor rule
        stops from being violated by a duplicate.
        """
        if len(cluster) == 1:
            return _as_fused(cluster[0])

        information = np.zeros((3, 3))
        weighted = np.zeros(3)
        for detection in cluster:
            inverse = np.linalg.inv(detection.covariance)
            information += inverse
            weighted += inverse @ detection.position_enu

        covariance = np.linalg.inv(information)
        position = covariance @ weighted

        contributing = tuple(detection.detection_id for detection in cluster)
        sensors = tuple(dict.fromkeys(detection.sensor_id for detection in cluster))

        return FusedDetection(
            # Derived from the parts rather than borrowing one of them, so a fused measurement
            # is never mistaken for the single detection whose id it would otherwise have taken.
            detection_id=f"{MULTI_SENSOR_MARKER}:{'+'.join(contributing)}",
            sensor_id=MULTI_SENSOR_MARKER,
            # The most recent contributing observation. Fusing across a cycle means the members
            # are within one tick of each other by construction, and taking the latest keeps the
            # fused measurement from ever looking older than a detection that went into it.
            timestamp=max(detection.timestamp for detection in cluster),
            position_enu=position,
            covariance=covariance,
            contributing_detection_ids=contributing,
            contributing_sensor_ids=sensors,
        )


def _as_fused(detection: IncomingDetection) -> FusedDetection:
    """Present one unreconciled detection in the fused shape, unchanged.

    Position, covariance and identifier are passed through untouched, so a single-sensor site
    gets bit-identical tracking whether fusion is enabled or not.
    """
    return FusedDetection(
        detection_id=detection.detection_id,
        sensor_id=detection.sensor_id,
        timestamp=detection.timestamp,
        position_enu=detection.position_enu,
        covariance=detection.covariance,
        contributing_detection_ids=(detection.detection_id,),
        contributing_sensor_ids=(detection.sensor_id,),
    )


def to_incoming(fused: FusedDetection) -> IncomingDetection:
    """The tracker's input type, from a fused measurement.

    The conversion exists so tracking's signature does not change. `IncomingDetection` is the
    type ADR 0002 uses to make the tracker structurally incapable of reading fields it must not
    read, and widening it to carry fusion provenance would weaken that guarantee to buy nothing
    — tracking has no use for the provenance, and the pipeline routes it to classification
    directly.
    """
    return IncomingDetection(
        detection_id=fused.detection_id,
        sensor_id=fused.sensor_id,
        timestamp=fused.timestamp,
        position_enu=fused.position_enu,
        covariance=fused.covariance,
    )
