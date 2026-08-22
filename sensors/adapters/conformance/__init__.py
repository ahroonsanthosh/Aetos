"""The Aetos sensor conformance suite.

Point it at any `SensorAdapter` — ours or a third party's — and it reports which floor that
source reaches and every requirement it failed, with reasons.

Shipped under `sensors/` rather than `tests/` on purpose. A customer's engineer integrating
their own hardware has to be able to run this against their own adapter without a copy of our
test suite, which is what makes "Aetos-compatible" a test result with a date on it rather than a
claim made in a meeting. See ADR 0017.

    from sensors.adapters.conformance import run_conformance

    report = run_conformance(adapter, thresholds=thresholds, clock=clock)
    print(report.render())

What this establishes, and what it does not, is stated at the bottom of every report it prints
and is worth reading before quoting a floor from one.
"""

from sensors.adapters.conformance.checks import PlacementContext
from sensors.adapters.conformance.report import (
    CheckOutcome,
    CheckResult,
    ConformanceReport,
)
from sensors.adapters.conformance.runner import DEFAULT_CYCLES, run_conformance

__all__ = [
    "DEFAULT_CYCLES",
    "CheckOutcome",
    "CheckResult",
    "ConformanceReport",
    "PlacementContext",
    "run_conformance",
]
