"""What a conformance run produces.

A report is a statement about one adapter at one moment, and it is careful about the difference
between three things that are easy to blur: a requirement that was checked and passed, one that
was checked and failed, and one that **could not be checked at all**.

The third is not a rounding error. A run that produced no detections cannot establish anything
about the detections an adapter emits, and reporting that as a pass would be exactly the
over-claim this whole mechanism exists to prevent. `NOT_CHECKABLE` is therefore a first-class
outcome, in the same way `NOT_BUILT` is a first-class component state in `backend/api/system.py`
and for the same reason: a green light that nothing computed is worse than no light at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sensors.adapters.capabilities import (
    FloorAssessment,
    FloorThresholds,
    SensorCapabilities,
    SensorFloor,
)


class CheckOutcome(StrEnum):
    """How one requirement came out."""

    PASS = "pass"
    FAIL = "fail"

    NOT_APPLICABLE = "not_applicable"
    """The requirement does not bind this source — a classification check on a source that does
    not classify. Not a gap; there is nothing to establish."""

    NOT_CHECKABLE = "not_checkable"
    """The requirement binds, and this run could not establish it either way.

    Distinct from NOT_APPLICABLE, and the distinction matters: this is a *gap in the evidence*,
    and a report carrying one has not shown what it might appear to have shown."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One requirement, one verdict, one explanation."""

    requirement: str
    """Identifier from `docs/sensor_interface_specification.md` — `R0.3`, `R1.1` — or a `D-` code
    for a declaration self-consistency check, which has no requirement number because it is not
    something a sensor does but something a declaration is."""

    title: str
    outcome: CheckOutcome

    detail: str
    """Always populated, including on a pass. A verdict with no explanation cannot be acted on,
    and a passing check that says what it actually established is what stops a reader assuming
    it established more."""

    @property
    def failed(self) -> bool:
        return self.outcome is CheckOutcome.FAIL


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """The result of running the suite against one adapter.

    Read the `declared_only` note before quoting a floor from this. The suite establishes that a
    source's *claims* are self-consistent, that it behaves as its claims imply, and that those
    claims clear the deployment's thresholds. It cannot establish that the claims are true — no
    software can determine that a sensor claiming three metres delivers it, and that is a
    measurement activity, not a test.
    """

    sensor_id: str
    capabilities: SensorCapabilities
    thresholds: FloorThresholds
    assessment: FloorAssessment
    results: tuple[CheckResult, ...]

    detections_observed: int
    """How many detections the run actually saw. Zero is why checks come back NOT_CHECKABLE, and
    it is reported so a reader can tell a clean run from an empty one."""

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.outcome is CheckOutcome.FAIL)

    @property
    def gaps(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.outcome is CheckOutcome.NOT_CHECKABLE)

    @property
    def conformant(self) -> bool:
        """Whether every checkable requirement passed and the source was admitted at all.

        Gaps do not make a report non-conformant — they make it incomplete, which is a different
        statement and is reported separately. A caller deciding whether to accept a sensor should
        look at both.
        """
        return not self.failures and self.assessment.floor.is_admitted

    @property
    def floor(self) -> SensorFloor:
        return self.assessment.floor

    def render(self) -> str:
        """A human-readable report. The thing a customer's engineer actually reads."""
        capabilities = self.capabilities
        assessment = self.assessment
        lines: list[str] = [
            f"Aetos sensor conformance — {self.sensor_id}",
            "=" * 78,
            f"  source           {capabilities.sensor_type.value}  ({capabilities.description})",
            f"  declared floor   {assessment.floor.label.upper()}"
            + (f"  (floor {assessment.floor.rung})" if assessment.floor.rung is not None else ""),
            f"  classifying      {'yes' if assessment.classifying else 'no'}",
            f"  discrimination   {assessment.discrimination_range_m:.0f} m"
            f"   (two objects {self.thresholds.minimum_separation_m:.0f} m apart)",
            f"  detections seen  {self.detections_observed}",
            "",
            "Thresholds this deployment applies",
            "-" * 78,
            f"  update period    <= {self.thresholds.update_period_ceiling_s:.2f} s"
            f"   (declared {capabilities.timing.update_period_s:.2f} s)",
            f"  latency          <= {self.thresholds.latency_ceiling_ms:.0f} ms"
            f"   (declared {capabilities.timing.max_latency_ms:.0f} ms)",
            f"  horizontal sigma <= {self.thresholds.position_uncertainty_ceiling_m:.1f} m"
            f" at {assessment.assessed_range_m:.0f} m"
            f"   (declared "
            f"{capabilities.accuracy.horizontal_uncertainty_at_m(assessment.assessed_range_m):.1f}"
            " m)",
            f"  timestamp sigma  <= {self.thresholds.timestamp_uncertainty_ceiling_ms:.0f} ms"
            f"   (declared {capabilities.timing.timestamp_uncertainty_ms:.0f} ms)",
            "",
            "Checks",
            "-" * 78,
        ]

        for result in self.results:
            marker = {
                CheckOutcome.PASS: "  ok  ",
                CheckOutcome.FAIL: " FAIL ",
                CheckOutcome.NOT_APPLICABLE: "  n/a ",
                CheckOutcome.NOT_CHECKABLE: " GAP  ",
            }[result.outcome]
            lines.append(f"{marker} {result.requirement:<6} {result.title}")
            if result.outcome is not CheckOutcome.PASS:
                lines.append(f"         {result.detail}")

        if assessment.shortfalls:
            lines.extend(["", "Why it does not reach the next floor", "-" * 78])
            lines.extend(f"  - {shortfall}" for shortfall in assessment.shortfalls)

        lines.extend(
            [
                "",
                "-" * 78,
                (
                    f"RESULT: {'conformant' if self.conformant else 'NOT conformant'} at "
                    f"{assessment.floor.label.upper()}"
                    + (f", with {len(self.gaps)} unchecked requirement(s)" if self.gaps else "")
                ),
                "",
                "This is DECLARED conformance. The suite establishes that this source's claims",
                "are self-consistent, that its behaviour matches them, and that they clear this",
                "deployment's thresholds. It does not and cannot establish that the claims are",
                "true: that requires measurement against a known reference, and is an",
                "integration activity. See docs/sensor_interface_specification.md section 8.",
            ]
        )
        return "\n".join(lines)
