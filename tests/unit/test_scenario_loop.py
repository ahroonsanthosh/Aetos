"""Looped scenarios, for an unattended hosted demonstration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scenarios.library import load_all
from scenarios.world import ScenarioWorld

EPOCH = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
GAP_S = 30.0


@pytest.fixture
def scenario():
    return load_all()["b_approaching_threat"]


def at(seconds: float) -> datetime:
    return EPOCH + timedelta(seconds=seconds)


def test_without_a_loop_the_sky_empties_and_stays_empty(scenario) -> None:
    world = ScenarioWorld(scenario, EPOCH)
    later = scenario.duration_s * 3

    assert world.objects_at(at(later)) == ()
    assert world.is_finished(at(later))


def test_a_looped_scenario_is_never_finished(scenario) -> None:
    world = ScenarioWorld(scenario, EPOCH, loop_gap_s=GAP_S)

    assert not world.is_finished(at(scenario.duration_s * 10))


def test_the_sky_is_empty_during_the_gap(scenario) -> None:
    world = ScenarioWorld(scenario, EPOCH, loop_gap_s=GAP_S)

    assert world.objects_at(at(scenario.duration_s + GAP_S / 2)) == ()


def test_the_second_run_replays_the_first(scenario) -> None:
    plain = ScenarioWorld(scenario, EPOCH)
    looped = ScenarioWorld(scenario, EPOCH, loop_gap_s=GAP_S)
    period = scenario.duration_s + GAP_S

    for t in (0.0, 10.0, 100.0, scenario.duration_s - 1):
        assert looped.objects_at(at(period + t)) == plain.objects_at(at(t))
        assert looped.objects_at(at(5 * period + t)) == plain.objects_at(at(t))


def test_a_negative_gap_is_refused(scenario) -> None:
    with pytest.raises(ValueError):
        ScenarioWorld(scenario, EPOCH, loop_gap_s=-1.0)
