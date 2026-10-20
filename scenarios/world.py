"""The scenario world: ground truth over time, and the only thing a simulated sensor may read.

`ScenarioWorld` implements `TruthSource`. It owns every object's flight path and the scripted
sensor availability, and it answers one question — what is in the air at this instant — in the
site's local ENU frame.

It is queried by wall time rather than by elapsed seconds so that every sensor sees the same
world at the same moment, whatever rate it polls at and whenever it happened to start. Sensors
disagreeing about where an object was is scenario G's subject; it must come from their sensor
models, not from the simulator's bookkeeping.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.common.geodesy import EnuPoint
from scenarios.schema import Scenario
from scenarios.trajectory import TRAJECTORY_STEP_S, ObjectTrajectory
from sensors.adapters.simulated.degradation import NOMINAL, SensorDegradation
from sensors.adapters.simulated.truth import TruthObject


class ScenarioWorld:
    """Ground truth for one run of one scenario.

    Construction integrates every trajectory once. That is deliberate: it happens before the
    clock starts, so the cost never lands inside an update cycle, and it means an unflyable
    scenario fails at load rather than several minutes into a run.
    """

    __slots__ = ("_scenario", "_epoch", "_trajectories")

    def __init__(
        self, scenario: Scenario, epoch: datetime, step_s: float = TRAJECTORY_STEP_S
    ) -> None:
        if epoch.tzinfo is None:
            raise ValueError("scenario epoch must be timezone-aware")

        self._scenario = scenario
        self._epoch = epoch.astimezone(UTC)
        self._trajectories = tuple(
            ObjectTrajectory(spec, scenario.duration_s, step_s) for spec in scenario.objects
        )

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    @property
    def epoch(self) -> datetime:
        return self._epoch

    def elapsed_s(self, when: datetime) -> float:
        """Seconds into the scenario. Negative before it starts."""
        return (when.astimezone(UTC) - self._epoch).total_seconds()

    def objects_at(self, when: datetime) -> tuple[TruthObject, ...]:
        """Every object in the air at `when`, in site-relative ENU metres."""
        elapsed = self.elapsed_s(when)
        objects: list[TruthObject] = []
        for trajectory in self._trajectories:
            state = trajectory.state_at(elapsed)
            if state is None:
                continue
            objects.append(
                TruthObject(
                    object_id=trajectory.object_id,
                    position=EnuPoint(east_m=state.east_m, north_m=state.north_m, up_m=state.up_m),
                    velocity_east_mps=state.velocity_east_mps,
                    velocity_north_mps=state.velocity_north_mps,
                    velocity_up_mps=state.velocity_up_mps,
                    label=trajectory.spec.true_classification,
                )
            )
        return tuple(objects)

    def degradation_at(self, sensor_id: str, when: datetime) -> SensorDegradation:
        """What the scenario says this sensor's performance should be at `when`.

        The last matching window wins, so a file can state a broad condition and then override
        part of it, in the order a reader encounters them. Nothing matching means nominal —
        a sensor is healthy unless the scenario says otherwise.
        """
        elapsed = self.elapsed_s(when)
        degradation = NOMINAL
        for window in self._scenario.sensor_availability:
            if window.sensor_id == sensor_id and window.covers(elapsed):
                degradation = window.as_degradation()
        return degradation

    def is_finished(self, when: datetime) -> bool:
        return self.elapsed_s(when) > self._scenario.duration_s
