"""The adapter: a scripted link at one end, canonical Detections at the other.

Every test here runs against `FakeTransport`, which is the point. The behaviours that matter
most — a frame arriving split across reads, a link that goes quiet, a link that starts
corrupting, a queue that fills — are the ones a real port makes hard to provoke deliberately and
easy to hit by accident on site.

The geometry tests assert against numbers worked out by hand rather than against whatever the
code produced. A bearing sign error or a swapped sine and cosine puts every detection in a
plausible-looking wrong place, which is the failure least likely to be noticed by eye.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from sensors.adapters.capabilities import TransportIntegrity
from sensors.adapters.interface import SensorAdapterError, SensorState
from sensors.adapters.ld2451.adapter import QUANTISATION_SIGMA, Ld2451Adapter
from sensors.adapters.ld2451.config import Ld2451DriverConfig
from sensors.adapters.ld2451.protocol import (
    DATA_FRAME_HEADER,
    TargetFrame,
    TargetReport,
    assumed_altitude_sigma_m,
    encode_target_frame,
)
from sensors.adapters.ld2451.transport import FakeTransport
from sensors.adapters.registry import load_sensor_registry

EPOCH = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ORIGIN = GeodeticPoint(latitude_deg=51.8971, longitude_deg=-8.4697, altitude_m=5.0)
SENSOR_ENU = EnuPoint(east_m=0.0, north_m=0.0, up_m=2.0)


@pytest.fixture(scope="module")
def entry():
    return load_sensor_registry()["ld2451_uart"]


@pytest.fixture(scope="module")
def frame() -> LocalFrame:
    return LocalFrame(ORIGIN)


def config(entry, **overrides) -> Ld2451DriverConfig:
    return Ld2451DriverConfig.model_validate({**entry.driver_config, **overrides})


def target(**overrides) -> TargetReport:
    defaults = {
        "angle_deg": 0,
        "distance_m": 40,
        "approaching": True,
        "speed_kmh": 18,
        "snr": 80,
    }
    return TargetReport(**{**defaults, **overrides})


def bytes_for(*targets: TargetReport, alarm: bool = True) -> bytes:
    return encode_target_frame(TargetFrame(alarm=alarm, targets=targets))


def build(entry, frame, chunks, *, clock=None, boresight_deg=0.0, **config_overrides):
    clock = clock or SimClock(EPOCH)
    adapter = Ld2451Adapter(
        clock=clock,
        sensor_id="radar_bench",
        model=entry,
        config=config(entry, **config_overrides),
        position_enu=SENSOR_ENU,
        frame=frame,
        boresight_deg=boresight_deg,
        transport_factory=lambda baud: FakeTransport(chunks, description=f"fake@{baud}"),
    )
    return adapter, clock


def drain(adapter, *, at_least: int = 1, timeout_s: float = 3.0) -> list:
    """Poll until the reader thread has produced enough, or give up.

    The reader is a real thread, so a single poll races it. Retrying to a deadline is what makes
    these tests deterministic without reaching inside the adapter to synchronise on its internals.
    """
    collected: list = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        collected.extend(adapter.get_detections())
        if len(collected) >= at_least:
            return collected
        time.sleep(0.005)
    return collected


def wait_for_state(adapter, state: SensorState, timeout_s: float = 3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        health = adapter.health()
        if health.state is state:
            return health
        time.sleep(0.005)
    return adapter.health()


# ------------------------------------------------------------------------------------------
# Establishing the link
# ------------------------------------------------------------------------------------------


def test_probing_settles_on_the_configured_rate_when_it_works(entry, frame) -> None:
    adapter, _ = build(entry, frame, [bytes_for(target())] * 20)
    adapter.start()
    try:
        assert adapter.baud_rate == entry.driver_config["baud_rate"]
    finally:
        adapter.stop()


def test_probing_falls_through_to_a_candidate_when_the_first_is_silent(entry, frame) -> None:
    """The wrong rate does not fail cleanly: it delivers bytes that frame up to nothing, which
    looks exactly like a powered sensor detecting nothing. So each rate is judged by whether a
    frame came out of it."""
    opened: list[int] = []

    def factory(baud: int) -> FakeTransport:
        opened.append(baud)
        # 115200 produces noise that never frames up; 256000 produces real frames.
        chunks = [bytes_for(target())] * 20 if baud == 256000 else [b"\x00\x11\x22" * 8] * 20
        return FakeTransport(chunks, description=f"fake@{baud}")

    adapter = Ld2451Adapter(
        clock=SimClock(EPOCH),
        sensor_id="radar_bench",
        model=entry,
        config=config(entry),
        position_enu=SENSOR_ENU,
        frame=frame,
        transport_factory=factory,
    )
    adapter.start()
    try:
        assert adapter.baud_rate == 256000
        assert opened == [115200, 256000]
    finally:
        adapter.stop()


def test_a_link_that_decodes_at_no_rate_refuses_to_start(entry, frame) -> None:
    """Named causes in the message, ordered by likelihood. A driver that failed with 'could not
    start' would send someone to the software for a wiring problem."""
    adapter, _ = build(entry, frame, [b"\x00" * 64] * 40)

    with pytest.raises(SensorAdapterError, match="decoded no frames at any candidate baud"):
        adapter.start()


# ------------------------------------------------------------------------------------------
# Geometry — worked out by hand
# ------------------------------------------------------------------------------------------


def test_a_target_ahead_lands_due_north_of_the_sensor(entry, frame) -> None:
    adapter, _ = build(entry, frame, [bytes_for(target(angle_deg=0, distance_m=40))] * 5)
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    enu = frame.to_enu(
        GeodeticPoint(
            latitude_deg=detection.position.latitude,
            longitude_deg=detection.position.longitude,
            altitude_m=detection.position.altitude_m,
        )
    )
    assert enu.north_m == pytest.approx(40.0, abs=0.05)
    assert enu.east_m == pytest.approx(0.0, abs=0.05)


@pytest.mark.parametrize(
    ("boresight", "angle", "expected_bearing"),
    [
        (0.0, 20, 20.0),
        (0.0, -20, 340.0),
        (90.0, 0, 90.0),
        (90.0, 15, 105.0),
        (350.0, 20, 10.0),
    ],
)
def test_bearing_is_boresight_plus_reported_angle(
    entry, frame, boresight, angle, expected_bearing
) -> None:
    """The device reports relative to its own boresight; the site says where that points. Getting
    the composition wrong rotates the whole picture, which looks plausible from the inside."""
    adapter, _ = build(
        entry,
        frame,
        [bytes_for(target(angle_deg=angle, distance_m=50))] * 5,
        boresight_deg=boresight,
    )
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    enu = frame.to_enu(
        GeodeticPoint(
            latitude_deg=detection.position.latitude,
            longitude_deg=detection.position.longitude,
            altitude_m=detection.position.altitude_m,
        )
    )
    bearing = math.degrees(math.atan2(enu.east_m, enu.north_m)) % 360.0
    assert bearing == pytest.approx(expected_bearing, abs=0.1)


def test_the_azimuth_sign_mirrors_the_picture(entry, frame) -> None:
    """The calibration this exists for. The protocol does not say whether a positive angle is
    clockwise or anticlockwise, and getting it wrong puts a target on the left at exactly the
    right range on the right — with no other symptom."""
    positions = {}
    for sign in (1, -1):
        adapter, _ = build(
            entry,
            frame,
            [bytes_for(target(angle_deg=20, distance_m=50))] * 5,
            azimuth_sign=sign,
        )
        adapter.start()
        try:
            detection = drain(adapter)[0]
        finally:
            adapter.stop()
        positions[sign] = frame.to_enu(
            GeodeticPoint(
                latitude_deg=detection.position.latitude,
                longitude_deg=detection.position.longitude,
                altitude_m=detection.position.altitude_m,
            )
        )

    assert positions[1].east_m == pytest.approx(-positions[-1].east_m, abs=0.05)
    assert positions[1].north_m == pytest.approx(positions[-1].north_m, abs=0.05)


# ------------------------------------------------------------------------------------------
# Altitude, uncertainty and motion
# ------------------------------------------------------------------------------------------


def test_altitude_is_marked_assumed_and_sits_in_the_sensors_own_plane(entry, frame) -> None:
    """The device has no elevation channel, so the figure is an assumption and must say so or
    the interface renders it as a measurement (ADR 0019)."""
    from sensors.schemas.detection import AltitudeSource

    adapter, _ = build(entry, frame, [bytes_for(target(distance_m=60))] * 5)
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    assert detection.position.altitude_source is AltitudeSource.ASSUMED
    assert detection.position.altitude_m == pytest.approx(
        ORIGIN.altitude_m + SENSOR_ENU.up_m, abs=0.1
    )


def test_the_vertical_variance_says_how_little_the_assumption_is_worth(entry, frame) -> None:
    """Uniform over the beam's vertical extent at that range: half-width over root three. About
    15 m at 100 m, which correctly tells the filter it has learned nothing about altitude."""
    adapter, _ = build(entry, frame, [bytes_for(target(distance_m=100))] * 5)
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    expected = assumed_altitude_sigma_m(100.0, 30.0)
    covariance = detection.measurement.position_covariance_enu
    assert math.sqrt(covariance[2][2]) == pytest.approx(expected, rel=1e-6)
    assert expected == pytest.approx(15.47, abs=0.05)


def test_the_scalar_uncertainty_never_understates_the_worst_axis(entry, frame) -> None:
    """A consumer reading only the scalar must not end up believing this sensor is better than
    it is in its weakest direction — which here is always the assumed vertical."""
    adapter, _ = build(entry, frame, [bytes_for(target(distance_m=80))] * 5)
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    import numpy as np

    covariance = np.array(detection.measurement.position_covariance_enu)
    worst = math.sqrt(float(np.linalg.eigvalsh(covariance).max()))
    assert detection.measurement.position_uncertainty_m == pytest.approx(worst, rel=1e-9)


def test_the_horizontal_covariance_is_the_sensor_ellipse_rotated(entry, frame) -> None:
    """Precise along the line of sight, imprecise across it. Collapsing that to something
    isotropic would discard exactly what keeps two crossing tracks apart."""
    import numpy as np

    adapter, _ = build(
        entry, frame, [bytes_for(target(angle_deg=0, distance_m=100))] * 5, boresight_deg=90.0
    )
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    covariance = np.array(detection.measurement.position_covariance_enu)
    sigma_range = math.hypot(1.0, QUANTISATION_SIGMA)
    sigma_cross = 100.0 * math.radians(math.hypot(3.0, QUANTISATION_SIGMA))

    # Boresight due east, so range error is on the east axis and cross-range on the north axis.
    assert math.sqrt(covariance[0][0]) == pytest.approx(sigma_range, rel=1e-6)
    assert math.sqrt(covariance[1][1]) == pytest.approx(sigma_cross, rel=1e-6)
    assert covariance[0][1] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize(
    ("approaching", "speed_kmh", "expected_mps"),
    [(True, 18, -5.0), (False, 18, 5.0), (True, 36, -10.0)],
)
def test_radial_velocity_is_negative_toward_the_sensor(
    entry, frame, approaching, speed_kmh, expected_mps
) -> None:
    """The schema's convention, and this device genuinely measures it — FMCW Doppler is why
    range rate is carried separately from speed and heading (ADR 0002)."""
    adapter, _ = build(
        entry,
        frame,
        [bytes_for(target(approaching=approaching, speed_kmh=speed_kmh))] * 5,
    )
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    assert detection.kinematics.radial_velocity_mps == pytest.approx(expected_mps, abs=1e-9)
    assert detection.kinematics.speed_mps is None, "speed is a tracker's job, not a sensor's"
    assert detection.kinematics.heading_deg is None
    assert detection.classification is None, "this device reports no identity evidence"


def test_confidence_rescales_the_devices_own_signal_to_noise(entry, frame) -> None:
    adapter, _ = build(entry, frame, [bytes_for(target(snr=75))] * 5)
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    assert detection.quality.confidence == pytest.approx(0.75)
    assert detection.quality.snr_db is None, (
        "the device documents 0-100, not decibels; putting it in a dB field would be a claim it "
        "never made"
    )


# ------------------------------------------------------------------------------------------
# Time
# ------------------------------------------------------------------------------------------


def test_observation_time_is_arrival_minus_flight_time(entry, frame) -> None:
    """A frame's last byte arriving is not the observation happening. Timing error is systematic
    and becomes position error at target speed, so it is reconstructed rather than approximated
    by 'now'."""
    clock = SimClock(EPOCH)
    adapter, _ = build(entry, frame, [bytes_for(target())] * 5, clock=clock)
    adapter.start()
    try:
        detection = drain(adapter)[0]
    finally:
        adapter.stop()

    transmission_s = (5 * 10 + 2 + 10) * 10 / 115200  # max frame at 10 targets, 8N1
    expected_lag = transmission_s + 30.0 / 1000.0
    lag = (clock.now() - detection.timestamp).total_seconds()

    # To the microsecond, which is `datetime`'s resolution and therefore the limit of what a
    # timestamp can express. Anything tighter would be asserting against the rounding.
    assert lag == pytest.approx(expected_lag, abs=1e-6)
    assert detection.timestamp < clock.now(), "an observation cannot be later than now"


# ------------------------------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------------------------------


def test_polling_before_start_raises(entry, frame) -> None:
    adapter, _ = build(entry, frame, [bytes_for(target())])

    with pytest.raises(SensorAdapterError, match="polled before start"):
        adapter.get_detections()


def test_a_stopped_adapter_reports_nothing_and_can_restart(entry, frame) -> None:
    adapter, _ = build(entry, frame, [bytes_for(target())] * 40)
    adapter.start()
    drain(adapter)
    adapter.stop()

    assert adapter.get_detections() == []
    assert adapter.health().state is SensorState.OFFLINE

    adapter.start()
    try:
        assert adapter.health().state is not SensorState.NOT_STARTED
    finally:
        adapter.stop()


def test_a_frame_split_across_reads_still_arrives(entry, frame) -> None:
    """Serial reads land wherever they land. The decoder handles the reassembly; this checks the
    adapter does not undo that by treating a read as a frame."""
    raw = bytes_for(target(distance_m=33))
    chunks = [raw[:4], raw[4:9], raw[9:]] * 10
    adapter, _ = build(entry, frame, chunks)
    adapter.start()
    try:
        detections = drain(adapter)
    finally:
        adapter.stop()

    assert detections


def test_a_full_queue_drops_the_oldest_and_counts_it(entry, frame) -> None:
    """Bounded, and never silent (ADR 0008). Oldest because a live picture wants the freshest
    observation and a stale detection would be gated out anyway."""
    adapter, _ = build(entry, frame, [bytes_for(target())] * 200, queue_capacity=5)
    adapter.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and adapter.frames_dropped == 0:
            time.sleep(0.005)
        dropped = adapter.frames_dropped
        health = adapter.health()
    finally:
        adapter.stop()

    assert dropped > 0
    assert health.state is SensorState.DEGRADED
    assert "dropped" in health.reason


# ------------------------------------------------------------------------------------------
# Trusting the link — ADR 0018's degradation half
# ------------------------------------------------------------------------------------------


def corrupt(raw: bytes) -> bytes:
    """A structurally perfect frame carrying an impossible speed. Nothing in the protocol
    catches this; only the plausibility envelope does."""
    mutated = bytearray(raw)
    mutated[len(DATA_FRAME_HEADER) + 2 + 2 + 3] = 250
    return bytes(mutated)


def test_a_link_that_starts_corrupting_reports_degraded(entry, frame) -> None:
    good = bytes_for(target())
    chunks = [good, corrupt(good)] * 60
    adapter, _ = build(entry, frame, chunks, degraded_reject_rate=0.05, offline_reject_rate=0.9)
    adapter.start()
    try:
        health = wait_for_state(adapter, SensorState.DEGRADED)
    finally:
        adapter.stop()

    assert health.state is SensorState.DEGRADED
    assert "rejected" in health.reason
    assert "undetectable" in health.reason, (
        "the reason must carry the estimated undetected rate, which is the point of measuring "
        "the observable one at all"
    )


def test_a_link_past_the_offline_threshold_emits_nothing(entry, frame) -> None:
    """A source that is probably lying is worse than one that is quiet. A quiet sensor produces
    coasting tracks and a visible gap; a lying one produces confident phantom targets."""
    good = bytes_for(target())
    chunks = [good] + [corrupt(good)] * 200
    adapter, _ = build(entry, frame, chunks, offline_reject_rate=0.2, degraded_reject_rate=0.05)
    adapter.start()
    try:
        health = wait_for_state(adapter, SensorState.OFFLINE)
        emitted = adapter.get_detections()
    finally:
        adapter.stop()

    assert health.state is SensorState.OFFLINE
    assert emitted == []
    assert "worse than one that is quiet" in health.reason


def test_a_silent_link_reports_offline_rather_than_an_empty_sky(entry, frame) -> None:
    """The distinction the adapter is allowed to make. This device transmits continuously when
    powered, so no bytes at all means the cable, the power or the port. Whether an empty *sky*
    is a fault is site policy and belongs to the watchdog (FM-010)."""
    clock = SimClock(EPOCH)
    adapter, _ = build(entry, frame, [bytes_for(target())] * 3, clock=clock)
    adapter.start()
    try:
        drain(adapter, timeout_s=1.0)
        clock.advance(10.0)
        health = wait_for_state(adapter, SensorState.OFFLINE)
    finally:
        adapter.stop()

    assert health.state is SensorState.OFFLINE
    assert "no bytes received" in health.reason
    assert "empty sky" in health.reason


def test_a_link_failure_is_reported_rather_than_raised(entry, frame) -> None:
    """One failed sensor must not stop the others. A dead link is a health fact, not a pipeline
    error (FM-011)."""
    adapter = Ld2451Adapter(
        clock=SimClock(EPOCH),
        sensor_id="radar_bench",
        model=entry,
        config=config(entry),
        position_enu=SENSOR_ENU,
        frame=frame,
        transport_factory=lambda baud: FakeTransport(
            [bytes_for(target())] * 3, fail_after=4, description="fake"
        ),
    )
    adapter.start()
    try:
        health = wait_for_state(adapter, SensorState.OFFLINE)
        assert adapter.get_detections() == []
    finally:
        adapter.stop()

    assert health.state is SensorState.OFFLINE
    assert "link failed" in health.reason


# ------------------------------------------------------------------------------------------
# What it declares
# ------------------------------------------------------------------------------------------


def test_capabilities_repeat_the_registry_and_compute_the_residual(entry, frame) -> None:
    """Everything but one number is the deployment's declaration, repeated rather than
    re-derived. The residual is computed, because asserting an integrity figure on a
    checksumless link rather than calculating it would make it worth nothing."""
    adapter, _ = build(entry, frame, [bytes_for(target())])
    capabilities = adapter.capabilities()

    assert capabilities.coverage == entry.coverage
    assert capabilities.timing == entry.timing
    assert capabilities.accuracy == entry.accuracy
    assert capabilities.dimensions == entry.dimensions
    assert capabilities.dimensions.reports_altitude is False
    assert capabilities.coverage_validated is False

    assert capabilities.transport.integrity is TransportIntegrity.STRUCTURAL
    assert capabilities.transport.residual_corruption_probability == pytest.approx(0.209, abs=0.01)


def test_capabilities_are_available_before_the_link_is_opened(entry, frame) -> None:
    """A customer is shown a declaration before anything is connected, which is exactly when it
    is most useful."""
    adapter, _ = build(entry, frame, [])
    assert adapter.capabilities().sensor_id == "radar_bench"


# ------------------------------------------------------------------------------------------
# Configurations that contradict the entry
# ------------------------------------------------------------------------------------------


def test_an_envelope_wider_than_the_declared_coverage_is_refused(entry, frame) -> None:
    """Otherwise the adapter emits detections outside its own declared coverage, and an operator
    reading an empty sector cannot rely on the coverage that emptiness is interpreted against."""
    with pytest.raises(ValueError, match="wider than the"):
        build(
            entry,
            frame,
            [],
            plausibility={**entry.driver_config["plausibility"], "max_azimuth_deg": 40},
        )


def test_an_envelope_reaching_past_the_declared_range_is_refused(entry, frame) -> None:
    with pytest.raises(ValueError, match="beyond the"):
        build(
            entry,
            frame,
            [],
            plausibility={**entry.driver_config["plausibility"], "max_distance_m": 200},
        )


def test_a_read_timeout_longer_than_an_update_period_is_refused(entry, frame) -> None:
    """Every frame in one read is timestamped from that read's completion, so a read spanning
    several updates gives frames that were seconds apart the same instant."""
    with pytest.raises(ValueError, match="update period"):
        build(entry, frame, [], read_timeout_s=0.5)


def test_a_degraded_threshold_above_the_offline_one_is_refused(entry) -> None:
    with pytest.raises(ValueError, match="must be below"):
        Ld2451DriverConfig.model_validate(
            {**entry.driver_config, "degraded_reject_rate": 0.5, "offline_reject_rate": 0.3}
        )
