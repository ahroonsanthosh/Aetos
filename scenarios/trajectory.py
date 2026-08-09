"""Trajectory integration — where a scenario object actually is at a given moment.

Two properties make this module worth having rather than inlining the arithmetic.

**The state at time t is a pure function of t.** It has to be. Four sensors polling at 1, 2, 4
and 10 Hz must all see the same object in the same place, and a driver that steps the clock
irregularly must not bend the flight path. So integration always runs on a fixed internal step
from the moment the object appears, independent of who is asking or how often.

**Motion is integrated, not interpolated between waypoints.** An object that jumps between
waypoints has infinite acceleration at every corner. A Kalman filter fed that produces
velocity estimates that are wrong in a way no real object could cause, and every tracking test
built on it measures the simulator rather than the tracker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from scenarios.schema import Motion, SimulatedObject

TRAJECTORY_STEP_S = 0.05
"""Integration step. Small enough that a fast turn advances heading by well under a degree per
step, so the accumulated path error over a five-minute scenario stays far below the smallest
sensor sigma in the registry — which is the only scale at which it could matter."""

_EAST, _NORTH, _UP, _VEAST, _VNORTH, _VUP = range(6)


@dataclass(frozen=True, slots=True)
class TrajectoryState:
    """An object's exact state at an instant, in site-relative ENU metres.

    Speed and heading are derived from the velocity rather than carried alongside it, so the
    two can never disagree, and so interpolation between samples does not have to deal with
    heading wrapping around north.
    """

    east_m: float
    north_m: float
    up_m: float
    velocity_east_mps: float
    velocity_north_mps: float
    velocity_up_mps: float

    @property
    def ground_speed_mps(self) -> float:
        return math.hypot(self.velocity_east_mps, self.velocity_north_mps)

    @property
    def heading_deg(self) -> float:
        """Degrees clockwise from north. Zero when stationary, which is a convention rather
        than a measurement — a motionless object has no heading."""
        if self.ground_speed_mps == 0.0:
            return 0.0
        return math.degrees(math.atan2(self.velocity_east_mps, self.velocity_north_mps)) % 360.0


class ObjectTrajectory:
    """One object's flight path, precomputed once and read by interpolation.

    Precomputed because the alternative — integrating from zero on every query — is quadratic
    in scenario length and would dominate the cost of a saturation scenario. Interpolation
    between samples is linear, over an interval short enough that the difference from the
    integrated path is orders of magnitude below any sensor's measurement noise.
    """

    __slots__ = ("_spec", "_start_s", "_end_s", "_step_s", "_samples")

    def __init__(
        self, spec: SimulatedObject, duration_s: float, step_s: float = TRAJECTORY_STEP_S
    ) -> None:
        if step_s <= 0.0:
            raise ValueError("trajectory step must be positive")

        self._spec = spec
        self._step_s = step_s
        self._start_s = spec.appears_at_s
        self._end_s = min(
            duration_s if spec.disappears_at_s is None else spec.disappears_at_s, duration_s
        )
        self._samples = _integrate(spec, self._end_s - self._start_s, step_s)

    @property
    def object_id(self) -> str:
        return self._spec.object_id

    @property
    def spec(self) -> SimulatedObject:
        return self._spec

    def is_present(self, elapsed_s: float) -> bool:
        """Whether the object is in the air at this point in the scenario.

        The interval is closed at both ends: an object that disappears at t=120 is still there
        at exactly 120, so a scenario written to have it leave the moment it clears the
        boundary does not lose its last observation to a floating-point comparison.
        """
        return self._start_s <= elapsed_s <= self._end_s

    def state_at(self, elapsed_s: float) -> TrajectoryState | None:
        """State at this point in the scenario, or None if the object is not present."""
        if not self.is_present(elapsed_s):
            return None

        position = (elapsed_s - self._start_s) / self._step_s
        lower = min(int(position), len(self._samples) - 1)
        upper = min(lower + 1, len(self._samples) - 1)
        fraction = position - lower

        row = self._samples[lower] + (self._samples[upper] - self._samples[lower]) * fraction
        return TrajectoryState(
            east_m=float(row[_EAST]),
            north_m=float(row[_NORTH]),
            up_m=float(row[_UP]),
            velocity_east_mps=float(row[_VEAST]),
            velocity_north_mps=float(row[_VNORTH]),
            velocity_up_mps=float(row[_VUP]),
        )


def _integrate(spec: SimulatedObject, span_s: float, step_s: float) -> np.ndarray:
    """Fixed-step integration of the motion model, from the moment the object appears.

    Heading and speed advance by half a step before the position update and half a step after,
    rather than all at once. Over a sustained turn the one-sided form biases the path
    consistently to one side of the true arc; splitting it cancels the first-order term for
    the cost of two extra additions per step.
    """
    motion = spec.motion
    steps = max(int(math.ceil(span_s / step_s)), 1)

    east = spec.start.east_m
    north = spec.start.north_m
    up = spec.start.altitude_m
    speed = motion.speed_mps
    heading = math.radians(motion.heading_deg)
    turn_rate = math.radians(motion.turn_rate_dps)

    samples = np.empty((steps + 1, 6), dtype=float)
    samples[0] = _row(east, north, up, speed, heading, motion.climb_rate_mps, motion.min_altitude_m)

    for index in range(1, steps + 1):
        heading += turn_rate * step_s / 2.0
        midpoint_speed = _clamp_speed(speed + motion.acceleration_mps2 * step_s / 2.0, motion)

        east += midpoint_speed * math.sin(heading) * step_s
        north += midpoint_speed * math.cos(heading) * step_s
        up = max(up + motion.climb_rate_mps * step_s, motion.min_altitude_m)

        heading += turn_rate * step_s / 2.0
        speed = _clamp_speed(speed + motion.acceleration_mps2 * step_s, motion)

        samples[index] = _row(
            east, north, up, speed, heading, motion.climb_rate_mps, motion.min_altitude_m
        )

    return samples


def _row(
    east: float,
    north: float,
    up: float,
    speed: float,
    heading: float,
    climb_rate: float,
    min_altitude: float,
) -> tuple[float, ...]:
    # An object resting on the altitude floor is not still descending, whatever its climb rate
    # says. Reporting the unclamped rate would give the filter a vertical velocity that
    # contradicts the position sequence it also sees.
    vertical = 0.0 if up <= min_altitude and climb_rate < 0.0 else climb_rate
    return (
        east,
        north,
        up,
        speed * math.sin(heading),
        speed * math.cos(heading),
        vertical,
    )


def _clamp_speed(speed: float, motion: Motion) -> float:
    speed = max(speed, 0.0)
    if motion.max_speed_mps is not None:
        speed = min(speed, motion.max_speed_mps)
    return speed
