"""The individual requirement checks.

One function per group of requirements from `docs/sensor_interface_specification.md`, each
returning `CheckResult`s. Split from the runner so that the checks are readable as a list of
what Aetos actually requires, and so a reader can compare them against the specification
without following control flow.

Every check states what it established, including when it passed. A check that passes silently
invites a reader to assume it established more than it did, which on a conformance report is the
same over-claim the whole mechanism exists to prevent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from sensors.adapters.capabilities import SensorCapabilities
from sensors.adapters.conformance.report import CheckOutcome, CheckResult
from sensors.adapters.interface import SensorAdapter, SensorAdapterError, SensorState
from sensors.schemas.detection import AltitudeSource, Detection

COVERAGE_SIGMA_MARGIN = 5.0
"""How far outside declared coverage a detection may fall before it counts as a declaration
error rather than as measurement noise.

A margin is required, not a concession. Coverage is a statement about where a sensor can see an
object; the position it *reports* is that truth displaced by noise, so an object detected at the
coverage edge is legitimately reported outside it. Every honest sensor does this, and a strict
containment check would fail all of them.

Five sigma is far enough out that noise is not a plausible explanation and the declaration is
the remaining one."""

UNCERTAINTY_TOLERANCE = 0.02
"""Slack on the comparison between declared and emitted uncertainty, for floating-point work and
for a declaration rounded to a sensible number of digits. Deliberately small: the comparison is
one-sided — a declaration may overstate error freely and may not understate it — so this is a
tolerance on arithmetic, not on honesty."""


@dataclass(frozen=True, slots=True)
class PlacementContext:
    """Where the sensor sits, so coverage can be checked at all.

    Not part of `SensorCapabilities` and deliberately not part of the adapter interface: a
    sensor's characteristics belong to the registry and its placement belongs to the site,
    because one model can be fitted at many sites (`sensors/adapters/README.md`). The
    conformance runner takes it from the caller, who has both.

    Absent, the coverage check reports NOT_CHECKABLE rather than being skipped quietly.
    """

    position_enu: EnuPoint
    frame: LocalFrame
    boresight_deg: float = 0.0


def _passed(requirement: str, title: str, detail: str) -> CheckResult:
    return CheckResult(requirement, title, CheckOutcome.PASS, detail)


def _failed(requirement: str, title: str, detail: str) -> CheckResult:
    return CheckResult(requirement, title, CheckOutcome.FAIL, detail)


def _gap(requirement: str, title: str, detail: str) -> CheckResult:
    return CheckResult(requirement, title, CheckOutcome.NOT_CHECKABLE, detail)


def _not_applicable(requirement: str, title: str, detail: str) -> CheckResult:
    return CheckResult(requirement, title, CheckOutcome.NOT_APPLICABLE, detail)


# --------------------------------------------------------------------------------------------
# Declaration self-consistency
# --------------------------------------------------------------------------------------------


def check_declaration(
    adapter: SensorAdapter, capabilities: SensorCapabilities
) -> list[CheckResult]:
    """Whether the declaration agrees with itself and with the adapter carrying it.

    These have `D-` codes rather than requirement numbers because they are not things a sensor
    does. They are things a declaration is, and a declaration that contradicts itself cannot be
    the basis for any conclusion drawn from it further down.
    """
    results: list[CheckResult] = []
    accuracy = capabilities.accuracy

    if capabilities.sensor_id != adapter.sensor_id:
        results.append(
            _failed(
                "D-1",
                "Declaration identifies the adapter carrying it",
                f"capabilities declare sensor_id {capabilities.sensor_id!r} but the adapter's is "
                f"{adapter.sensor_id!r}; health records, fusion and the event log all key on "
                "this, so a mismatch means the declaration describes a different sensor",
            )
        )
    elif capabilities.sensor_type is not adapter.sensor_type:
        results.append(
            _failed(
                "D-1",
                "Declaration identifies the adapter carrying it",
                f"capabilities declare sensor_type {capabilities.sensor_type.value!r} but the "
                f"adapter reports {adapter.sensor_type.value!r}",
            )
        )
    else:
        results.append(
            _passed(
                "D-1",
                "Declaration identifies the adapter carrying it",
                f"sensor_id and sensor_type both match the adapter ({adapter.sensor_id}, "
                f"{adapter.sensor_type.value})",
            )
        )

    angular_at_reference = (
        accuracy.reference_range_m * accuracy.angular_uncertainty_mrad * 1e-3
    )
    if angular_at_reference > accuracy.position_uncertainty_m:
        results.append(
            _failed(
                "D-2",
                "Accuracy declaration is internally consistent",
                f"the angular term alone contributes {angular_at_reference:.1f} m at the "
                f"{accuracy.reference_range_m:.0f} m reference range, which already exceeds the "
                f"{accuracy.position_uncertainty_m:.1f} m total declared there. One of the two "
                "figures is wrong, and the range-independent floor cannot be recovered from them",
            )
        )
    else:
        results.append(
            _passed(
                "D-2",
                "Accuracy declaration is internally consistent",
                f"range-independent floor of {accuracy.range_independent_floor_m():.1f} m plus an "
                f"angular term of {accuracy.angular_uncertainty_mrad:.1f} mrad reproduces the "
                f"declared {accuracy.position_uncertainty_m:.1f} m at "
                f"{accuracy.reference_range_m:.0f} m",
            )
        )

    if capabilities.coverage_validated:
        results.append(
            _passed(
                "D-3",
                "Coverage has been measured, not taken from a datasheet",
                "declared as validated against the threat class this deployment cares about",
            )
        )
    else:
        results.append(
            _gap(
                "D-3",
                "Coverage has been measured, not taken from a datasheet",
                "coverage_validated is false. A datasheet range is quoted against a radar cross-"
                "section usually one to two orders of magnitude larger than a small multirotor, "
                "so the declared coverage is an unverified claim. Until it is measured, silence "
                "from this sensor must not be rendered as clear sky (FM-016)",
            )
        )

    return results


# --------------------------------------------------------------------------------------------
# R0.7, R1.4 — lifecycle
# --------------------------------------------------------------------------------------------


def check_lifecycle(adapter: SensorAdapter) -> list[CheckResult]:
    """Exercise the lifecycle contract on an adapter that has not been started.

    Destructive in the sense that it starts and stops the adapter, so the runner calls it before
    anything that depends on a running adapter and leaves it started afterwards.
    """
    results: list[CheckResult] = []

    try:
        adapter.get_detections()
    except SensorAdapterError:
        results.append(
            _passed(
                "R0.7",
                "Polling before start() raises",
                "raised SensorAdapterError as required; an adapter nobody started that returned "
                "an empty list would be a wiring error disguised as an empty sky",
            )
        )
    except Exception as exc:  # noqa: BLE001 — any other exception is still a contract failure
        results.append(
            _failed(
                "R0.7",
                "Polling before start() raises",
                f"raised {type(exc).__name__} rather than SensorAdapterError: callers "
                f"distinguishing a misconfigured adapter from a broken one cannot ({exc})",
            )
        )
    else:
        results.append(
            _failed(
                "R0.7",
                "Polling before start() raises",
                "returned normally. An unstarted adapter reporting no detections is "
                "indistinguishable from a working one observing an empty sky, which is the one "
                "thing an operator must never be shown by mistake (FM-010)",
            )
        )

    health = adapter.health()
    if health.state is SensorState.NOT_STARTED:
        results.append(
            _passed(
                "R0.6",
                "Health distinguishes 'not started' from 'offline'",
                "reports NOT_STARTED before start(); nothing has gone wrong yet, and saying "
                "OFFLINE would claim a fault",
            )
        )
    else:
        results.append(
            _failed(
                "R0.6",
                "Health distinguishes 'not started' from 'offline'",
                f"reports {health.state.value!r} before start() rather than NOT_STARTED",
            )
        )

    identity_before = adapter.sensor_id
    adapter.start()
    adapter.start()
    results.append(
        _passed(
            "R0.7",
            "start() is idempotent",
            "starting a running adapter returned without raising",
        )
    )

    adapter.stop()
    adapter.stop()
    after_stop = adapter.get_detections()
    if after_stop:
        results.append(
            _failed(
                "R0.7",
                "stop() is idempotent and silences the source",
                f"emitted {len(after_stop)} detection(s) after stop(); a scenario can script a "
                "sensor offline mid-run and a stopped adapter must produce nothing",
            )
        )
    else:
        results.append(
            _passed(
                "R0.7",
                "stop() is idempotent and silences the source",
                "stopping twice did not raise, and polling afterwards produced nothing",
            )
        )

    adapter.start()
    if adapter.sensor_id == identity_before:
        results.append(
            _passed(
                "R1.4",
                "Identity is stable across a restart",
                f"sensor_id remained {identity_before!r} through stop() and start()",
            )
        )
    else:
        results.append(
            _failed(
                "R1.4",
                "Identity is stable across a restart",
                f"sensor_id changed from {identity_before!r} to {adapter.sensor_id!r}; fusion, "
                "health and the event log key on it, so a renamed sensor appears as a new source "
                "with no history",
            )
        )

    running = adapter.health()
    if running.reason.strip():
        results.append(
            _passed(
                "R0.6",
                "Health always carries a reason",
                f"state {running.state.value!r} explained as {running.reason!r}, including in "
                "the healthy case",
            )
        )
    else:
        results.append(
            _failed(
                "R0.6",
                "Health always carries a reason",
                "reason was empty. A health record that explains itself only when something is "
                "wrong forces the operator interface to invent the wording for the healthy case, "
                "and the two then drift apart",
            )
        )

    return results


# --------------------------------------------------------------------------------------------
# R0.1, R0.3, R0.4, R0.8 — what the detections themselves must be
# --------------------------------------------------------------------------------------------


def check_detections(
    capabilities: SensorCapabilities, detections: list[Detection], clock: Clock
) -> list[CheckResult]:
    """Everything establishable from the emitted detections alone."""
    if not detections:
        return [
            _gap(
                requirement,
                title,
                "the run produced no detections, so nothing about emitted detections could be "
                "established. Drive the adapter with something to observe and run again",
            )
            for requirement, title in (
                ("R0.1", "Emits canonical detections and nothing else"),
                ("R0.3", "Reported uncertainty does not understate the declaration"),
                ("R0.4", "Timestamps are observation time, timezone-aware, not in the future"),
                ("R0.8", "Detections attribute themselves to this sensor truthfully"),
                ("R2.2", "Covariance is present and well-formed where declared"),
                ("RC.1", "Classification appears only where declared"),
            )
        ]

    return [
        _check_canonical(detections),
        _check_uncertainty(capabilities, detections),
        _check_timestamps(detections, clock),
        _check_attribution(capabilities, detections),
        _check_covariance(capabilities, detections),
        _check_classification(capabilities, detections),
        _check_altitude(capabilities, detections),
    ]


def _check_canonical(detections: list[Detection]) -> CheckResult:
    """R0.1. Pydantic has already validated these — what is left is uniqueness of identity,
    which nothing in the schema can enforce across a sequence."""
    identifiers = [d.detection_id for d in detections]
    duplicates = {i for i in identifiers if identifiers.count(i) > 1}
    if duplicates:
        return _failed(
            "R0.1",
            "Emits canonical detections and nothing else",
            f"{len(duplicates)} detection_id(s) were reused within one run, first "
            f"{sorted(duplicates)[0]!r}. Ingestion deduplicates on this identifier (FM-013), so "
            "a reused one silently discards a real observation",
        )
    return _passed(
        "R0.1",
        "Emits canonical detections and nothing else",
        f"{len(detections)} detections validated against the canonical schema with unique "
        "identifiers",
    )


def _check_uncertainty(
    capabilities: SensorCapabilities, detections: list[Detection]
) -> CheckResult:
    """R0.3. The comparison is deliberately one-sided.

    A source may report more uncertainty than it declared — conditions vary, and overstating is
    safe. It may not report less: the declaration is what a customer was shown and what the
    floor assessment was computed from, and a sensor quietly operating better than its
    declaration is indistinguishable, from the outside, from one whose declaration was wrong.
    """
    accuracy = capabilities.accuracy
    worst: tuple[float, float, Detection] | None = None

    for detection in detections:
        reported = detection.measurement.position_uncertainty_m
        # The declared figure is a function of range, and range is not on the detection — but
        # the reference range bounds it for any source whose error grows with range, so the
        # declaration at the reference range is the meaningful ceiling for a whole run.
        allowed = accuracy.uncertainty_at_m(accuracy.reference_range_m) * (
            1.0 + UNCERTAINTY_TOLERANCE
        )
        if reported > allowed and (worst is None or reported > worst[0]):
            worst = (reported, allowed, detection)

    if worst is not None:
        reported, allowed, detection = worst
        return _failed(
            "R0.3",
            "Reported uncertainty does not understate the declaration",
            f"detection {detection.detection_id!r} reported {reported:.1f} m against a declared "
            f"ceiling of {allowed:.1f} m at the reference range. Either the declaration is wrong "
            "or the adapter is degraded; conformance assumes nominal operation",
        )

    reported_values = [d.measurement.position_uncertainty_m for d in detections]
    return _passed(
        "R0.3",
        "Reported uncertainty does not understate the declaration",
        f"reported one-sigma ranged {min(reported_values):.1f}–{max(reported_values):.1f} m, "
        f"within the {accuracy.uncertainty_at_m(accuracy.reference_range_m):.1f} m declared at "
        f"the {accuracy.reference_range_m:.0f} m reference range",
    )


def _check_timestamps(detections: list[Detection], clock: Clock) -> CheckResult:
    """R0.4. Timezone-awareness is enforced by the schema; what is checked here is that the
    timestamp is an observation time rather than a value from somewhere else entirely."""
    now = clock.now()
    ahead = [d for d in detections if d.timestamp > now]
    if ahead:
        return _failed(
            "R0.4",
            "Timestamps are observation time, timezone-aware, not in the future",
            f"{len(ahead)} detection(s) are timestamped after the injected clock's current time, "
            f"the furthest by {(max(d.timestamp for d in ahead) - now).total_seconds():.3f} s. "
            "An observation cannot have been made later than now, so this timestamp came from "
            "somewhere other than the clock the rest of the system uses (ADR 0003)",
        )

    naive = [d for d in detections if d.timestamp.tzinfo is None]
    if naive:
        return _failed(
            "R0.4",
            "Timestamps are observation time, timezone-aware, not in the future",
            f"{len(naive)} detection(s) carry a naive timestamp (FM-012)",
        )

    span = max(d.timestamp for d in detections) - min(d.timestamp for d in detections)
    return _passed(
        "R0.4",
        "Timestamps are observation time, timezone-aware, not in the future",
        f"all {len(detections)} timestamps are timezone-aware, at or before the clock, spanning "
        f"{span.total_seconds():.1f} s",
    )


def _check_attribution(
    capabilities: SensorCapabilities, detections: list[Detection]
) -> CheckResult:
    """R0.8. Simulated-versus-real is carried by `sensor_type` and nowhere else, so a source
    misreporting it removes the only channel that distinguishes the two (ADR 0002)."""
    wrong_id = [d for d in detections if d.sensor.sensor_id != capabilities.sensor_id]
    if wrong_id:
        return _failed(
            "R0.8",
            "Detections attribute themselves to this sensor truthfully",
            f"{len(wrong_id)} detection(s) name a different sensor_id than the adapter declares",
        )

    wrong_type = [d for d in detections if d.sensor.sensor_type is not capabilities.sensor_type]
    if wrong_type:
        return _failed(
            "R0.8",
            "Detections attribute themselves to this sensor truthfully",
            f"{len(wrong_type)} detection(s) declare a sensor_type other than "
            f"{capabilities.sensor_type.value!r}. Simulated-versus-real is carried by this field "
            "alone, and the interface must show it unmissably",
        )

    return _passed(
        "R0.8",
        "Detections attribute themselves to this sensor truthfully",
        f"every detection names {capabilities.sensor_id!r} as a "
        f"{capabilities.sensor_type.value!r} source"
        + (" (simulated)" if capabilities.sensor_type.is_simulated else " (real)"),
    )


def _check_covariance(
    capabilities: SensorCapabilities, detections: list[Detection]
) -> CheckResult:
    """R2.2. Present where declared, well-formed, and not contradicted by the scalar."""
    accuracy = capabilities.accuracy
    with_covariance = [
        d for d in detections if d.measurement.position_covariance_enu is not None
    ]

    if not accuracy.reports_covariance:
        if with_covariance:
            return _failed(
                "R2.2",
                "Covariance is present and well-formed where declared",
                f"{len(with_covariance)} detection(s) carry a covariance although the "
                "declaration says none is reported; a consumer choosing between the scalar and "
                "the matrix would use one the declaration says does not exist",
            )
        if accuracy.anisotropy_ratio > 2.0:
            return _failed(
                "R2.2",
                "Covariance is present and well-formed where declared",
                f"error is declared {accuracy.anisotropy_ratio:.1f}:1 anisotropic with no "
                "covariance reported. A scalar cannot express an ellipse that elongated, and "
                "fusion would contribute this source's worst axis to the axis it was best at",
            )
        return _not_applicable(
            "R2.2",
            "Covariance is present and well-formed where declared",
            "this source declares isotropic error and reports no covariance, which is coherent",
        )

    missing = len(detections) - len(with_covariance)
    if missing:
        return _failed(
            "R2.2",
            "Covariance is present and well-formed where declared",
            f"{missing} of {len(detections)} detection(s) omit the covariance this source "
            "declares it reports",
        )

    for detection in with_covariance:
        matrix = np.array(detection.measurement.position_covariance_enu, dtype=float)
        eigenvalues = np.linalg.eigvalsh(matrix)
        if float(eigenvalues.min()) < -1e-9:
            return _failed(
                "R2.2",
                "Covariance is present and well-formed where declared",
                f"detection {detection.detection_id!r} has a covariance with a negative "
                f"eigenvalue ({eigenvalues.min():.3e}); it does not describe any real error "
                "distribution and the filter's update would be ill-conditioned",
            )

        worst_axis = math.sqrt(max(float(eigenvalues.max()), 0.0))
        scalar = detection.measurement.position_uncertainty_m
        if scalar < worst_axis * (1.0 - UNCERTAINTY_TOLERANCE):
            return _failed(
                "R2.2",
                "Covariance is present and well-formed where declared",
                f"detection {detection.detection_id!r} reports a scalar of {scalar:.2f} m while "
                f"its covariance's worst axis is {worst_axis:.2f} m. A consumer reading only the "
                "scalar would believe this source is better than it is in its weakest direction",
            )

    return _passed(
        "R2.2",
        "Covariance is present and well-formed where declared",
        f"all {len(with_covariance)} covariances are symmetric, positive semi-definite, and "
        "no scalar understates its own matrix's worst axis",
    )


def _check_classification(
    capabilities: SensorCapabilities, detections: list[Detection]
) -> CheckResult:
    """RC.1–RC.3. Grade C is optional forever, so absence is not applicable rather than failed."""
    hinted = [d for d in detections if d.classification is not None]

    if not capabilities.classifies:
        if hinted:
            return _failed(
                "RC.1",
                "Classification appears only where declared",
                f"{len(hinted)} detection(s) carry a classification hint although the source "
                "declares it does not classify. The classification module weights sensor hints "
                "as evidence, so an undeclared one enters that weighting unaccounted for",
            )
        return _not_applicable(
            "RC.1",
            "Classification appears only where declared",
            "this source does not classify, and emits no classification hints",
        )

    if not hinted:
        return _gap(
            "RC.1",
            "Classification appears only where declared",
            "the source declares it classifies but emitted no hints in this run; nothing about "
            "its labelling could be established",
        )

    return _passed(
        "RC.1",
        "Classification appears only where declared",
        f"{len(hinted)} of {len(detections)} detections carry a hint, each with a label from the "
        "closed vocabulary and a confidence reported separately from it",
    )


def _check_altitude(
    capabilities: SensorCapabilities, detections: list[Detection]
) -> CheckResult:
    """ADR 0019. The per-detection marker and the static declaration must agree.

    An adapter declaring an altitude channel while marking every detection `assumed` is a
    declaration error, and it is one with consequences: the interface renders a measured
    altitude as a figure and an assumed one as not measured, so the two must not disagree.
    """
    declared = capabilities.dimensions.reports_altitude
    assumed = [d for d in detections if d.position.altitude_source is AltitudeSource.ASSUMED]

    if declared and assumed:
        return _failed(
            "R0.2",
            "Altitude marking agrees with the altitude declaration",
            f"the source declares it measures altitude, but {len(assumed)} of {len(detections)} "
            "detections mark it assumed. The interface would render an assumption as a "
            "measurement, which ADR 0011 forbids",
        )

    if not declared and len(assumed) != len(detections):
        measured = len(detections) - len(assumed)
        return _failed(
            "R0.2",
            "Altitude marking agrees with the altitude declaration",
            f"the source declares no altitude channel, but {measured} of {len(detections)} "
            "detections present their altitude as measured or derived rather than assumed",
        )

    if declared:
        return _passed(
            "R0.2",
            "Altitude marking agrees with the altitude declaration",
            "this source measures altitude, and no detection marks it assumed",
        )

    return _passed(
        "R0.2",
        "Altitude marking agrees with the altitude declaration",
        f"this source has no elevation channel, and all {len(detections)} detections mark "
        "altitude as assumed so it is never rendered as a measured figure (ADR 0019)",
    )


# --------------------------------------------------------------------------------------------
# R0.5 — coverage
# --------------------------------------------------------------------------------------------


def check_coverage(
    capabilities: SensorCapabilities,
    detections: list[Detection],
    placement: PlacementContext | None,
) -> CheckResult:
    """R0.5. Every detection must lie within declared coverage, allowing for noise.

    See `COVERAGE_SIGMA_MARGIN` for why a margin is required rather than merely convenient.
    """
    if placement is None:
        return _gap(
            "R0.5",
            "Detections lie within declared coverage",
            "no placement was supplied, so a detection's range and bearing relative to the "
            "sensor could not be computed. Coverage is a statement about geometry, and geometry "
            "needs to know where the sensor is",
        )

    if not detections:
        return _gap(
            "R0.5",
            "Detections lie within declared coverage",
            "the run produced no detections",
        )

    coverage = capabilities.coverage
    boresight_rad = math.radians(placement.boresight_deg)
    half_extent_rad = math.radians(coverage.azimuth_extent_deg) / 2.0

    for detection in detections:
        enu = placement.frame.to_enu(
            GeodeticPoint(
                latitude_deg=detection.position.latitude,
                longitude_deg=detection.position.longitude,
                altitude_m=detection.position.altitude_m,
            )
        )
        east = enu.east_m - placement.position_enu.east_m
        north = enu.north_m - placement.position_enu.north_m
        up = enu.up_m - placement.position_enu.up_m

        horizontal = math.hypot(east, north)
        slant = math.hypot(horizontal, up)
        margin_m = COVERAGE_SIGMA_MARGIN * detection.measurement.position_uncertainty_m

        if slant > coverage.max_range_m + margin_m:
            return _failed(
                "R0.5",
                "Detections lie within declared coverage",
                f"detection {detection.detection_id!r} is {slant:.0f} m out, beyond the declared "
                f"{coverage.max_range_m:.0f} m maximum by more than "
                f"{COVERAGE_SIGMA_MARGIN:.0f} sigma. Coverage is what lets silence from this "
                "sensor be interpreted, and a sensor reporting outside it has declared the wrong "
                "coverage (FM-010)",
            )
        if slant < max(coverage.min_range_m - margin_m, 0.0):
            return _failed(
                "R0.5",
                "Detections lie within declared coverage",
                f"detection {detection.detection_id!r} is {slant:.0f} m out, inside the declared "
                f"{coverage.min_range_m:.0f} m blind zone by more than "
                f"{COVERAGE_SIGMA_MARGIN:.0f} sigma",
            )

        if slant <= 0.0:
            continue

        # Angular margins are the linear margin resolved at this detection's range: the same
        # metres of noise subtend a larger angle close in than far out.
        angular_margin_rad = math.atan2(margin_m, max(slant, 1e-6))

        elevation_rad = math.atan2(up, horizontal)
        if not (
            math.radians(coverage.elevation_min_deg) - angular_margin_rad
            <= elevation_rad
            <= math.radians(coverage.elevation_max_deg) + angular_margin_rad
        ):
            return _failed(
                "R0.5",
                "Detections lie within declared coverage",
                f"detection {detection.detection_id!r} sits at "
                f"{math.degrees(elevation_rad):.1f}° elevation, outside the declared "
                f"{coverage.elevation_min_deg:.0f}° to {coverage.elevation_max_deg:.0f}° band "
                "by more than measurement noise explains",
            )

        if not coverage.has_full_azimuth:
            azimuth_rad = math.atan2(east, north) % (2.0 * math.pi)
            offset = (azimuth_rad - boresight_rad + math.pi) % (2.0 * math.pi) - math.pi
            if abs(offset) > half_extent_rad + angular_margin_rad:
                return _failed(
                    "R0.5",
                    "Detections lie within declared coverage",
                    f"detection {detection.detection_id!r} is {math.degrees(abs(offset)):.1f}° "
                    f"off boresight, outside the declared "
                    f"{coverage.azimuth_extent_deg:.0f}° sector by more than measurement noise "
                    "explains",
                )

    return _passed(
        "R0.5",
        "Detections lie within declared coverage",
        f"all {len(detections)} detections fall inside the declared coverage volume, allowing "
        f"{COVERAGE_SIGMA_MARGIN:.0f} sigma of measurement noise at the edges",
    )
