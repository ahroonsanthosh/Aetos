"""Kalman filter: the measurement model, convergence, coasting and numerical hygiene."""

from __future__ import annotations

import numpy as np
import pytest

from backend.tracking.filters.kalman import (
    POSITION,
    VELOCITY,
    ConstantVelocityFilter,
    FilterConfig,
    isotropic_covariance,
)


@pytest.fixture
def kalman() -> ConstantVelocityFilter:
    return ConstantVelocityFilter()


R = isotropic_covariance(10.0)


# --------------------------------------------------------------------------------------
# The measurement model — ADR 0002 expressed in code
# --------------------------------------------------------------------------------------


def test_observation_matrix_cannot_see_velocity(kalman: ConstantVelocityFilter) -> None:
    """The structural guarantee behind ADR 0002.

    The velocity columns of H are zero, so there is no path by which a detection's kinematics
    block could enter the estimate even if a caller tried to supply one. Velocity is inferred
    from a sequence of positions, exactly as it must be with a real sensor that does not
    measure heading.
    """
    assert kalman._H.shape == (3, 6)
    assert np.array_equal(kalman._H[:, POSITION], np.eye(3))
    assert not kalman._H[:, VELOCITY].any()


def test_update_signature_accepts_position_only(kalman: ConstantVelocityFilter) -> None:
    """There is deliberately no parameter for a measured speed or heading."""
    parameters = set(ConstantVelocityFilter.update.__annotations__)

    assert parameters == {"state", "measured_position_enu", "measurement_covariance", "return"}


def test_initial_velocity_is_zero_with_wide_uncertainty(kalman: ConstantVelocityFilter) -> None:
    """A first detection carries no motion information. Claiming otherwise would make the
    filter reject the second detection of a fast object as an outlier."""
    state = kalman.initiate(np.array([100.0, 200.0, 50.0]), R)

    assert np.array_equal(state.velocity, np.zeros(3))
    assert state.covariance[3, 3] > state.covariance[0, 0]


def test_initial_position_uncertainty_comes_from_the_sensor(
    kalman: ConstantVelocityFilter,
) -> None:
    """A precise sensor should produce a correspondingly tight initial track, rather than
    every track starting at the same hardcoded uncertainty."""
    precise = kalman.initiate(np.zeros(3), isotropic_covariance(2.0))
    vague = kalman.initiate(np.zeros(3), isotropic_covariance(50.0))

    assert precise.covariance[0, 0] < vague.covariance[0, 0]


# --------------------------------------------------------------------------------------
# Convergence
# --------------------------------------------------------------------------------------


def _run_constant_velocity(
    kalman: ConstantVelocityFilter,
    truth_velocity: np.ndarray,
    *,
    seed: int,
    steps: int = 60,
    dt: float = 0.5,
    sigma_m: float = 10.0,
):
    rng = np.random.default_rng(seed)
    state = kalman.initiate(np.zeros(3), isotropic_covariance(sigma_m))
    position = np.zeros(3)

    for _ in range(steps):
        position = position + truth_velocity * dt
        measured = position + rng.normal(0.0, sigma_m, 3)
        state = kalman.update(
            kalman.predict(state, dt), measured, isotropic_covariance(sigma_m)
        )

    return state


def test_velocity_converges_from_positions_alone(kalman: ConstantVelocityFilter) -> None:
    """The central claim: the filter recovers velocity it was never told, from noisy
    positions. If this fails, tracking has no honest way to produce speed and heading.

    Compared against a naive two-point difference rather than an arbitrary tolerance, because
    two-point differencing is precisely what the filter has to beat to be worth having.
    """
    truth_velocity = np.array([25.0, -15.0, 0.0])
    dt, sigma_m = 0.5, 10.0

    state = _run_constant_velocity(kalman, truth_velocity, seed=20260808, dt=dt, sigma_m=sigma_m)

    naive_sigma = sigma_m * np.sqrt(2) / dt
    error = float(np.linalg.norm(state.velocity - truth_velocity))

    assert error < naive_sigma / 5.0


def test_filter_uncertainty_is_honest(kalman: ConstantVelocityFilter) -> None:
    """Filter consistency, which matters more than raw accuracy.

    A Kalman filter's covariance is a claim about its own error. If the claim is optimistic,
    every consumer downstream inherits the overconfidence: the association gate closes too
    tightly and rejects valid detections, and the operator is shown a precision that is not
    real. So the test is not 'is the estimate close' but 'is the estimate as close as the
    filter says it is' — checked across several seeds, since a single run can be lucky.
    """
    truth_velocity = np.array([25.0, -15.0, 0.0])

    for seed in range(12):
        state = _run_constant_velocity(kalman, truth_velocity, seed=seed)

        sigma = np.sqrt(np.diag(state.covariance[VELOCITY, VELOCITY]))
        normalised_error = np.abs(state.velocity - truth_velocity) / sigma

        assert normalised_error.max() < 3.0, (
            f"seed {seed}: error {normalised_error.max():.2f} sigma exceeds the filter's own "
            "claimed uncertainty — the covariance is optimistic"
        )


def test_speed_is_derived_from_the_velocity_estimate(kalman: ConstantVelocityFilter) -> None:
    truth_velocity = np.array([25.0, -15.0, 0.0])

    state = _run_constant_velocity(kalman, truth_velocity, seed=1)

    assert state.speed_mps == pytest.approx(float(np.linalg.norm(truth_velocity)), rel=0.25)


def test_position_uncertainty_shrinks_with_observation(kalman: ConstantVelocityFilter) -> None:
    state = kalman.initiate(np.zeros(3), R)
    initial = state.covariance[0, 0]

    for i in range(20):
        state = kalman.update(kalman.predict(state, 1.0), np.array([i * 10.0, 0.0, 0.0]), R)

    assert state.covariance[0, 0] < initial


def test_a_precise_measurement_moves_the_estimate_more_than_a_vague_one(
    kalman: ConstantVelocityFilter,
) -> None:
    """The Kalman gain doing its job: trust a measurement in proportion to its stated
    uncertainty. This is why an adapter that understates uncertainty is dangerous."""
    state = kalman.initiate(np.zeros(3), isotropic_covariance(20.0))
    measurement = np.array([100.0, 0.0, 0.0])

    precise = kalman.update(state, measurement, isotropic_covariance(1.0))
    vague = kalman.update(state, measurement, isotropic_covariance(200.0))

    assert precise.position[0] > vague.position[0]


def test_anisotropic_measurement_constrains_only_the_precise_axis(
    kalman: ConstantVelocityFilter,
) -> None:
    """The reason position_covariance_enu exists in the canonical schema. A radar precise in
    range and vague in cross-range must tighten the estimate along one axis only."""
    state = kalman.initiate(np.zeros(3), isotropic_covariance(50.0))
    anisotropic = np.diag([4.0, 2500.0, 100.0])  # precise east, vague north

    updated = kalman.update(state, np.array([100.0, 100.0, 0.0]), anisotropic)

    assert updated.covariance[0, 0] < updated.covariance[1, 1]


# --------------------------------------------------------------------------------------
# Prediction and coasting
# --------------------------------------------------------------------------------------


def test_prediction_extrapolates_along_the_velocity_estimate(
    kalman: ConstantVelocityFilter,
) -> None:
    state = kalman.initiate(np.zeros(3), R)
    state.mean[VELOCITY] = np.array([10.0, 0.0, 0.0])

    predicted = kalman.predict(state, 5.0)

    assert predicted.position == pytest.approx([50.0, 0.0, 0.0])


def test_uncertainty_grows_while_coasting(kalman: ConstantVelocityFilter) -> None:
    """A coasting track must become visibly less certain, not merely keep reporting a
    position. This growth is what later lets the association gate legitimately accept a
    reacquisition further away (FM-022)."""
    state = kalman.initiate(np.zeros(3), R)

    variances = []
    for _ in range(5):
        state = kalman.predict(state, 2.0)
        variances.append(state.covariance[0, 0])

    assert variances == sorted(variances)
    assert variances[-1] > variances[0] * 2


def test_prediction_has_no_side_effects(kalman: ConstantVelocityFilter) -> None:
    """The same state is predicted forward at several horizons when drawing a predicted
    trajectory, so prediction must not mutate its input."""
    state = kalman.initiate(np.array([1.0, 2.0, 3.0]), R)
    before = state.mean.copy()

    kalman.predict(state, 10.0)

    assert np.array_equal(state.mean, before)


def test_zero_dt_prediction_is_a_copy(kalman: ConstantVelocityFilter) -> None:
    state = kalman.initiate(np.array([1.0, 2.0, 3.0]), R)

    predicted = kalman.predict(state, 0.0)

    assert np.array_equal(predicted.mean, state.mean)
    assert predicted is not state


def test_backwards_prediction_is_refused(kalman: ConstantVelocityFilter) -> None:
    """Out-of-order detections (FM-012) must not silently rewind a track."""
    state = kalman.initiate(np.zeros(3), R)

    with pytest.raises(ValueError, match="backwards"):
        kalman.predict(state, -1.0)


# --------------------------------------------------------------------------------------
# Numerical hygiene and bounds
# --------------------------------------------------------------------------------------


def test_covariance_stays_symmetric_over_many_cycles(kalman: ConstantVelocityFilter) -> None:
    """Floating-point drift compounds. An asymmetric covariance produces a Mahalanobis
    distance that is not a distance, which silently corrupts the association gate."""
    rng = np.random.default_rng(7)
    state = kalman.initiate(np.zeros(3), R)

    for i in range(500):
        state = kalman.update(
            kalman.predict(state, 0.1), np.array([i * 1.0, 0.0, 0.0]) + rng.normal(0, 5, 3), R
        )

    assert np.allclose(state.covariance, state.covariance.T, atol=1e-12)


def test_covariance_stays_positive_definite(kalman: ConstantVelocityFilter) -> None:
    """What the Joseph-form update buys. A covariance with a negative eigenvalue is not a
    covariance, and the gate built on it stops meaning anything."""
    state = kalman.initiate(np.zeros(3), R)

    for i in range(200):
        state = kalman.update(kalman.predict(state, 0.05), np.array([i * 0.5, 0.0, 0.0]), R)

    assert np.linalg.eigvalsh(state.covariance).min() > 0.0


def test_velocity_is_clamped_to_something_physical(kalman: ConstantVelocityFilter) -> None:
    """FM-012 and FM-013: duplicate or badly time-stamped detections can otherwise drive the
    estimate to absurd velocities, poisoning every later prediction and gate."""
    state = kalman.initiate(np.zeros(3), R)

    # A jump of 50 km in a tenth of a second: physically impossible, and exactly what a
    # detection with a corrupted timestamp looks like.
    state = kalman.update(kalman.predict(state, 0.1), np.array([50_000.0, 0.0, 0.0]), R)

    assert state.speed_mps <= kalman.config.max_speed_mps + 1e-9


def test_clamping_preserves_direction(kalman: ConstantVelocityFilter) -> None:
    """Clamping magnitude while keeping bearing leaves the track recoverable, where zeroing
    the velocity would throw away the one thing still known about its motion."""
    config = FilterConfig(max_speed_mps=50.0)
    filt = ConstantVelocityFilter(config)
    state = filt.initiate(np.zeros(3), R)
    state.mean[VELOCITY] = np.array([300.0, 400.0, 0.0])

    bounded = filt.predict(state, 0.0)
    bounded = filt._bounded(bounded)

    assert bounded.speed_mps == pytest.approx(50.0)
    assert bounded.velocity[1] / bounded.velocity[0] == pytest.approx(400.0 / 300.0)


# --------------------------------------------------------------------------------------
# Mahalanobis distance — the input to the association gate
# --------------------------------------------------------------------------------------


def test_mahalanobis_is_zero_at_the_prediction(kalman: ConstantVelocityFilter) -> None:
    state = kalman.initiate(np.array([10.0, 20.0, 30.0]), R)

    assert kalman.mahalanobis_squared(state, np.array([10.0, 20.0, 30.0]), R) == pytest.approx(0.0)


def test_mahalanobis_accounts_for_track_uncertainty(kalman: ConstantVelocityFilter) -> None:
    """The property Euclidean distance cannot express, and the reason coasting tracks can be
    reacquired: an uncertain track should find a distant detection less surprising."""
    certain = kalman.initiate(np.zeros(3), isotropic_covariance(1.0))
    uncertain = kalman.initiate(np.zeros(3), isotropic_covariance(100.0))
    detection = np.array([50.0, 0.0, 0.0])

    assert kalman.mahalanobis_squared(certain, detection, R) > kalman.mahalanobis_squared(
        uncertain, detection, R
    )


def test_mahalanobis_is_direction_sensitive_under_anisotropy(
    kalman: ConstantVelocityFilter,
) -> None:
    """Equal Euclidean distances are not equally surprising when uncertainty differs by axis.
    This is what keeps a radar's cross-range vagueness from being treated as precision."""
    state = kalman.initiate(np.zeros(3), np.diag([4.0, 2500.0, 100.0]))

    along_precise = kalman.mahalanobis_squared(state, np.array([30.0, 0.0, 0.0]), R)
    along_vague = kalman.mahalanobis_squared(state, np.array([0.0, 30.0, 0.0]), R)

    assert along_precise > along_vague


def test_isotropic_covariance_rejects_non_positive_uncertainty() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        isotropic_covariance(0.0)
