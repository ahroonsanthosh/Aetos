"""Constant-velocity Kalman filter in the local ENU frame.

State is six-dimensional: position and velocity in East, North and Up, metres and metres per
second. All maths happens in the site's local tangent plane (ADR 0004) — never in degrees.

**The measurement model is position only.** This is not an omission, it is ADR 0002 expressed
in code: the observation matrix maps state to position and nothing else, so there is no path
by which a detection's `kinematics` block could enter the estimate even if a caller passed
one. Velocity is inferred from a sequence of positions, exactly as it would have to be with a
real sensor that does not measure heading.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# State vector layout. Kept as named slices rather than magic indices, because an off-by-one
# here produces a filter that is subtly wrong rather than obviously broken.
POSITION = slice(0, 3)
VELOCITY = slice(3, 6)
STATE_DIM = 6
MEASUREMENT_DIM = 3

MIN_SLANT_RANGE_M = 50.0
"""Range below which a Doppler measurement is declined rather than applied.

Range rate is a nonlinear function of state, and its Jacobian with respect to position carries
a factor of 1/range. Close in, that term explodes: at 0.5 m with a 50 m/s target the position
row reaches ~100 per second, the linearisation is valid over a span far smaller than the
track's own positional uncertainty, and the update stops being merely imprecise and starts
injecting large spurious position corrections.

Physically the measurement is worth little there anyway — at short range the range rate says
almost nothing about the *direction* of travel. Declining is therefore the honest answer, not
a limitation being worked around.
"""


class DopplerUnobservable(ValueError):
    """The geometry does not support a range-rate update at this instant.

    Raised rather than returned so the caller must decide what to do. `TrackManager` absorbs
    it and lets the position update stand alone, which is the correct behaviour: the detection
    is still good, only its range rate is unusable.
    """


@dataclass(frozen=True, slots=True)
class FilterConfig:
    """Tuning for the motion model.

    `process_noise_psd` is the power spectral density of the white-noise acceleration driving
    the constant-velocity model — in effect, how much manoeuvring the filter expects. Too low
    and the filter lags a turning object and rejects its detections at the gate; too high and
    it chases measurement noise and its uncertainty never shrinks enough to discriminate
    between nearby tracks. It is configuration rather than a constant because the right value
    depends on what is being tracked.
    """

    process_noise_psd: float = 4.0
    """Continuous-time acceleration PSD, m²/s³."""

    initial_position_variance_m2: float = 100.0
    initial_velocity_variance_m2s2: float = 400.0
    """Initial velocity uncertainty is deliberately large. On a first detection we know a
    position and nothing at all about motion, and claiming otherwise would make the filter
    reject the second detection of a fast object as an outlier."""

    max_speed_mps: float = 150.0
    """Sanity bound on the velocity estimate. A filter fed duplicate or badly time-stamped
    detections (FM-012, FM-013) can otherwise produce physically impossible velocities that
    then poison the prediction and the gate."""


@dataclass(slots=True)
class KalmanState:
    """Filter state: mean and covariance.

    Mutable, unlike the domain models, because it is updated in place many times per second
    and is internal to the tracker. It never leaves this module unconverted — the immutable
    `Track` is what gets published.
    """

    mean: np.ndarray
    """Shape (6,): [east, north, up, v_east, v_north, v_up]."""

    covariance: np.ndarray
    """Shape (6, 6)."""

    @property
    def position(self) -> np.ndarray:
        return self.mean[POSITION]

    @property
    def velocity(self) -> np.ndarray:
        return self.mean[VELOCITY]

    @property
    def speed_mps(self) -> float:
        return float(np.linalg.norm(self.mean[VELOCITY]))

    @property
    def position_covariance(self) -> np.ndarray:
        return self.covariance[POSITION, POSITION]

    def copy(self) -> KalmanState:
        return KalmanState(self.mean.copy(), self.covariance.copy())


class ConstantVelocityFilter:
    """A constant-velocity Kalman filter over ENU position and velocity.

    Constant velocity rather than constant acceleration because at V0 the objects of interest
    spend most of their time in steady flight, and a CV model with adequately sized process
    noise handles gentle manoeuvres without the extra states that a CA model needs to
    estimate — states which, on sparse noisy position measurements, mostly estimate noise.
    An interacting-multiple-model filter is the upgrade path if manoeuvre tracking becomes
    the binding constraint; the interface here does not preclude it.
    """

    def __init__(self, config: FilterConfig | None = None) -> None:
        self._config = config or FilterConfig()

        # Observation matrix: state -> position. Three rows, six columns, and the velocity
        # columns are structurally zero. See the module docstring.
        self._H = np.zeros((MEASUREMENT_DIM, STATE_DIM))
        self._H[:, POSITION] = np.eye(3)

    @property
    def config(self) -> FilterConfig:
        return self._config

    def initiate(self, position_enu: np.ndarray, measurement_covariance: np.ndarray) -> KalmanState:
        """Start a new filter from a single detection.

        Velocity is initialised to zero with large uncertainty rather than guessed. A first
        detection carries no motion information, and the honest representation of that is a
        wide velocity covariance — which is also what lets the second detection, wherever it
        falls within reason, be accepted and actually inform the estimate.
        """
        mean = np.zeros(STATE_DIM)
        mean[POSITION] = position_enu

        covariance = np.zeros((STATE_DIM, STATE_DIM))
        # Seed positional uncertainty from what the sensor actually reported rather than from
        # a constant, so a precise sensor produces a correspondingly tight initial track.
        covariance[POSITION, POSITION] = measurement_covariance
        covariance[VELOCITY, VELOCITY] = np.eye(3) * self._config.initial_velocity_variance_m2s2

        return KalmanState(mean, covariance)

    def predict(self, state: KalmanState, dt_s: float) -> KalmanState:
        """Advance state by `dt_s` seconds under the constant-velocity model.

        Returns a new state; the input is not modified. Prediction is used both to propagate
        between detections and to coast a track through a gap, so it must be free of side
        effects — the same state is predicted forward repeatedly at different horizons when
        drawing a predicted trajectory.
        """
        if dt_s < 0.0:
            raise ValueError("cannot predict backwards in time")
        if dt_s == 0.0:
            return state.copy()

        F = self._transition_matrix(dt_s)
        Q = self._process_noise(dt_s)

        mean = F @ state.mean
        covariance = F @ state.covariance @ F.T + Q

        return self._bounded(KalmanState(mean, _symmetrise(covariance)))

    def update(
        self,
        state: KalmanState,
        measured_position_enu: np.ndarray,
        measurement_covariance: np.ndarray,
    ) -> KalmanState:
        """Correct state with a measured position.

        Note the signature: a position and its covariance. There is deliberately no parameter
        through which a caller could supply a measured speed or heading (ADR 0002).
        """
        innovation, S = self.innovation(state, measured_position_enu, measurement_covariance)

        # Solve rather than invert: forming S^-1 explicitly is both slower and numerically
        # worse, and the covariance is small enough that the difference is free to take.
        kalman_gain = np.linalg.solve(S, self._H @ state.covariance).T

        mean = state.mean + kalman_gain @ innovation

        # Joseph form rather than the textbook (I - KH)P. It costs an extra matrix product and
        # guarantees the result stays symmetric positive-definite under floating-point error,
        # which the simple form does not — and a covariance that drifts asymmetric eventually
        # produces a Mahalanobis distance that is not a distance, silently corrupting the gate.
        identity = np.eye(STATE_DIM)
        factor = identity - kalman_gain @ self._H
        covariance = (
            factor @ state.covariance @ factor.T
            + kalman_gain @ measurement_covariance @ kalman_gain.T
        )

        return self._bounded(KalmanState(mean, _symmetrise(covariance)))

    def update_radial_velocity(
        self,
        state: KalmanState,
        measured_mps: float,
        variance_m2s2: float,
        sensor_position_enu: np.ndarray,
    ) -> KalmanState:
        """Correct state with a directly measured range rate.

        A *second, separate* update applied after the positional one, not a fourth row bolted
        onto it. Processing independent measurements sequentially is mathematically identical
        to one stacked update when their noise is uncorrelated — and position noise and Doppler
        noise are independent in every sensor model here — so this buys the equivalence at a
        fraction of the disruption. `H`, `MEASUREMENT_DIM`, `innovation`, `mahalanobis_squared`
        and the association gate are all untouched and stay purely positional.

        Gating deliberately stays positional too. A range-rate gate would reject a legitimately
        manoeuvring track, and range rate carries almost no discrimination between two contacts
        at similar bearings anyway.

        Linearising after the position update rather than before is not incidental: the
        posterior position is the best estimate available, so it is the best point about which
        to linearise, and the first-order error is correspondingly smaller.

        Raises `DopplerUnobservable` when the geometry cannot support the update; the caller is
        expected to let the position update stand alone.
        """
        if variance_m2s2 <= 0.0:
            raise ValueError("radial velocity variance must be positive")

        row, predicted = radial_velocity_jacobian(state.mean, sensor_position_enu)

        # Scalar measurement, so the innovation covariance is a float and the gain is a vector.
        # No linear solve, no matrix inverse.
        covariance_row = state.covariance @ row
        innovation_variance = float(row @ covariance_row) + variance_m2s2
        if innovation_variance <= 0.0 or not np.isfinite(innovation_variance):
            raise DopplerUnobservable("range-rate innovation covariance is not positive")

        gain = covariance_row / innovation_variance
        mean = state.mean + gain * (measured_mps - predicted)

        # Joseph form, for the same reason as the positional update: it stays symmetric and
        # positive semi-definite under finite precision, and this update writes straight into
        # the velocity block that every subsequent prediction and gate depends on.
        factor = np.eye(STATE_DIM) - np.outer(gain, row)
        covariance = (
            factor @ state.covariance @ factor.T + np.outer(gain, gain) * variance_m2s2
        )

        if not np.all(np.isfinite(mean)) or not np.all(np.isfinite(covariance)):
            # Refusing a bad update is always better than poisoning the track with NaN, which
            # would propagate through every future prediction and never clear.
            raise DopplerUnobservable("range-rate update produced a non-finite state")

        # _bounded matters more here than anywhere: this is the only update that writes
        # directly into the velocity block.
        return self._bounded(KalmanState(mean, _symmetrise(covariance)))

    def innovation(
        self,
        state: KalmanState,
        measured_position_enu: np.ndarray,
        measurement_covariance: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Measurement residual and its covariance.

        Exposed because the association stage needs exactly this to compute Mahalanobis
        distance (ADR 0005), and recomputing it there would risk the gate and the update
        disagreeing about the same pairing.
        """
        innovation = measured_position_enu - self._H @ state.mean
        S = self._H @ state.covariance @ self._H.T + measurement_covariance
        return innovation, _symmetrise(S)

    def mahalanobis_squared(
        self,
        state: KalmanState,
        measured_position_enu: np.ndarray,
        measurement_covariance: np.ndarray,
    ) -> float:
        """Squared Mahalanobis distance from a track's prediction to a measurement.

        This, not Euclidean distance, is what the association gate uses. It weights each axis
        by how uncertain the filter actually is along it, so a track that has been coasting
        legitimately accepts detections further away while a well-observed one does not.
        Euclidean distance cannot express that, and it is precisely the information that
        disambiguates two objects at a crossing.
        """
        innovation, S = self.innovation(state, measured_position_enu, measurement_covariance)
        solved = np.linalg.solve(S, innovation)
        return float(innovation @ solved)

    def _transition_matrix(self, dt_s: float) -> np.ndarray:
        F = np.eye(STATE_DIM)
        F[POSITION, VELOCITY] = np.eye(3) * dt_s
        return F

    def _process_noise(self, dt_s: float) -> np.ndarray:
        """Discrete white-noise acceleration covariance.

        The standard continuous-time result for a constant-velocity model driven by white
        acceleration: position variance grows as dt³/3, velocity as dt, with dt²/2 coupling
        between them. Using a diagonal approximation instead would understate how strongly
        position and velocity errors are correlated after a long gap, which is exactly the
        situation a coasting track is in.
        """
        q = self._config.process_noise_psd
        dt2, dt3 = dt_s**2, dt_s**3

        Q = np.zeros((STATE_DIM, STATE_DIM))
        Q[POSITION, POSITION] = np.eye(3) * (dt3 / 3.0)
        Q[POSITION, VELOCITY] = np.eye(3) * (dt2 / 2.0)
        Q[VELOCITY, POSITION] = np.eye(3) * (dt2 / 2.0)
        Q[VELOCITY, VELOCITY] = np.eye(3) * dt_s
        return Q * q

    def _bounded(self, state: KalmanState) -> KalmanState:
        """Clamp the velocity estimate to something physically possible.

        Duplicate detections or badly time-stamped ones (FM-012, FM-013) can drive the
        velocity estimate to absurd values, and an absurd velocity poisons every subsequent
        prediction and gate. Clamping the magnitude while preserving direction keeps the track
        recoverable rather than letting it fly off and be lost.
        """
        speed = float(np.linalg.norm(state.mean[VELOCITY]))
        if speed > self._config.max_speed_mps:
            state.mean[VELOCITY] *= self._config.max_speed_mps / speed
        return state


def radial_velocity_jacobian(
    state_mean: np.ndarray, sensor_position_enu: np.ndarray
) -> tuple[np.ndarray, float]:
    """The measurement row for range rate, and the rate this state predicts.

    Range rate is `r_dot = (p - s) . v / |p - s|` — the component of velocity along the line of
    sight from sensor `s` to target `p`. Unlike position it is *nonlinear* in the state, so it
    cannot be expressed as a constant row of `H` and must be linearised about the current
    estimate:

        d(r_dot)/dp = (v - r_dot * u) / rho     u = (p - s) / rho, rho = |p - s|
        d(r_dot)/dv = u

    The position term is the component of velocity perpendicular to the line of sight, divided
    by range: moving the target sideways changes how fast the range closes, and the closer it
    is the more a given sideways step matters. That 1/rho is why `MIN_SLANT_RANGE_M` exists.

    Returns the row and the predicted rate together because both come from the same three
    quantities and computing them separately would mean deriving `u` twice.
    """
    offset = state_mean[POSITION] - sensor_position_enu
    slant_range = float(np.linalg.norm(offset))
    if not np.isfinite(slant_range) or slant_range < MIN_SLANT_RANGE_M:
        raise DopplerUnobservable(
            f"slant range {slant_range:.1f} m is below the {MIN_SLANT_RANGE_M} m at which a "
            "range-rate measurement can be linearised usefully"
        )

    unit = offset / slant_range
    velocity = state_mean[VELOCITY]
    predicted = float(unit @ velocity)

    row = np.zeros(STATE_DIM)
    row[POSITION] = (velocity - predicted * unit) / slant_range
    row[VELOCITY] = unit
    return row, predicted


def isotropic_covariance(sigma_m: float) -> np.ndarray:
    """A 3x3 measurement covariance from a single one-sigma value.

    The fallback for sources that report only `position_uncertainty_m`. It asserts equal
    uncertainty in every direction, which for most real sensors is not true — hence the
    optional full covariance in the canonical schema, which is preferred whenever present.
    """
    if sigma_m <= 0.0:
        raise ValueError("measurement uncertainty must be positive")
    return np.eye(3) * (sigma_m**2)


def _symmetrise(matrix: np.ndarray) -> np.ndarray:
    """Force exact symmetry.

    Floating-point error makes covariance products drift asymmetric by tiny amounts that
    compound over thousands of update cycles. Averaging with the transpose costs almost
    nothing and keeps the matrix a valid covariance.
    """
    return (matrix + matrix.T) * 0.5
