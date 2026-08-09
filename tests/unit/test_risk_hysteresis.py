"""Risk level hysteresis: the boundary behaviour ADR 0007 names as its acceptance test.

The failure this machinery exists to prevent is not a wrong level. It is forty alerts for one
object, and the operator desensitisation that follows — which is worse than the flapping that
caused it. So the tests here are mostly about what *does not* happen.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.risk.hysteresis import HysteresisPolicy, LevelBand, LevelResolver
from backend.tracking.models import RiskLevel

START = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def policy() -> HysteresisPolicy:
    return HysteresisPolicy(
        escalate_dwell_s=2.0,
        deescalate_dwell_s=20.0,
        bands=[
            LevelBand(level=RiskLevel.LOW, rise=20, fall=14),
            LevelBand(level=RiskLevel.MEDIUM, rise=45, fall=37),
            LevelBand(level=RiskLevel.HIGH, rise=70, fall=61),
            LevelBand(level=RiskLevel.CRITICAL, rise=87, fall=79),
        ],
    )


@pytest.fixture
def resolver(policy: HysteresisPolicy) -> LevelResolver:
    return LevelResolver(policy)


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def test_a_first_assessment_adopts_its_level_immediately(resolver: LevelResolver) -> None:
    """Nothing to flap against yet, and a track that appears already close and closing is
    exactly the one whose alert must not be delayed by four cycles of climbing."""
    state = resolver.resolve(92, None, START)

    assert state.level is RiskLevel.CRITICAL


def test_a_score_drifting_inside_the_band_does_not_move_the_level(
    resolver: LevelResolver,
) -> None:
    """The Schmitt trigger. HIGH is entered at 70 and left at 61; between those the level
    holds whatever it already was, whatever the raw score is doing."""
    state = resolver.resolve(72, None, START)
    assert state.level is RiskLevel.HIGH

    for elapsed, score in enumerate([68, 64, 66, 62, 69, 63], start=1):
        state = resolver.resolve(score, state, at(elapsed * 30))
        assert state.level is RiskLevel.HIGH, f"level moved at score {score}"


def test_leaving_requires_crossing_the_lower_threshold(resolver: LevelResolver) -> None:
    state = resolver.resolve(72, None, START)
    held = resolver.resolve(62, state, at(60))
    dropped = resolver.resolve(60, held, at(120))

    assert held.level is RiskLevel.HIGH
    assert dropped.level is RiskLevel.MEDIUM


def test_oscillation_across_a_boundary_produces_no_level_changes(
    resolver: LevelResolver,
) -> None:
    """The scenario in ADR 0007: an object holding station just outside the protected zone,
    with a score sitting on a threshold. This is what forty alerts would look like."""
    state = resolver.resolve(71, None, START)
    changes = 0

    for cycle in range(60):
        score = 71 if cycle % 2 == 0 else 66
        updated = resolver.resolve(score, state, at(cycle * 0.5))
        if updated.level is not state.level:
            changes += 1
        state = updated

    assert changes == 0, f"level changed {changes} times on a score oscillating within one band"


def test_escalation_is_fast_and_deescalation_is_slow(resolver: LevelResolver) -> None:
    """Asymmetric by design. A genuine threat must not be held back by a timer; a single quiet
    cycle must not clear an elevated level."""
    state = resolver.resolve(30, None, START)
    assert state.level is RiskLevel.LOW  # above LOW's rise of 20, below MEDIUM's of 45

    escalated = resolver.resolve(90, state, at(3))
    assert escalated.level is RiskLevel.CRITICAL, "escalation should clear a 2 s dwell"

    too_soon = resolver.resolve(5, escalated, at(8))
    assert too_soon.level is RiskLevel.CRITICAL, "de-escalation should not clear a 20 s dwell"

    eventually = resolver.resolve(5, escalated, at(40))
    assert eventually.level is RiskLevel.NONE


def test_escalation_still_requires_its_dwell(resolver: LevelResolver) -> None:
    """Short, but not zero: one noisy update must not promote a benign object."""
    state = resolver.resolve(10, None, START)
    immediate = resolver.resolve(95, state, at(0.5))

    assert immediate.level is RiskLevel.NONE


def test_the_level_is_not_stepped_one_band_at_a_time(resolver: LevelResolver) -> None:
    """A score jumping straight to critical arrives at critical.

    Stepping would take four cycles to believe the clearest case in the system, which is the
    alert arriving late precisely when it matters most. The dwell timer already bounds the
    rate of change.
    """
    state = resolver.resolve(5, None, START)
    jumped = resolver.resolve(95, state, at(5))

    assert jumped.level is RiskLevel.CRITICAL


def test_the_changed_at_stamp_only_moves_on_a_real_change(resolver: LevelResolver) -> None:
    """The dwell clock has to measure time *at a level*, not time since the last assessment —
    otherwise a track assessed every half second can never accumulate any dwell at all."""
    state = resolver.resolve(72, None, START)
    later = resolver.resolve(71, state, at(30))

    assert later.changed_at == state.changed_at


def test_a_band_with_no_hysteresis_gap_is_rejected() -> None:
    """Inverted thresholds would let a level be entered and left at the same score, which is
    the flapping this whole mechanism exists to prevent — so it fails loudly at load rather
    than behaving strangely in the field."""
    with pytest.raises(ValueError, match="fall threshold"):
        LevelBand(level=RiskLevel.HIGH, rise=60, fall=70)


def test_a_score_below_every_band_is_none(resolver: LevelResolver) -> None:
    assert resolver.resolve(0, None, START).level is RiskLevel.NONE


def test_level_state_survives_a_gap_in_assessment(resolver: LevelResolver) -> None:
    """A track that coasts and is reassessed much later should not be held by a stale dwell."""
    state = resolver.resolve(90, None, START)
    much_later = resolver.resolve(10, state, at(600))

    assert much_later.level is RiskLevel.NONE
