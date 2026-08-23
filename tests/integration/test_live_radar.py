"""A real driver, end to end, over a scripted link.

The keystone. Phases 1 to 4 built a compatibility floor, a conformance suite and an adapter
factory with nothing real behind them; this is the test that says the whole apparatus works on a
driver that talks to hardware — that the floor admits it, the suite measures it, the factory
builds it, and the pipeline forms tracks from what comes off the wire.

The link is `FakeTransport` rather than a serial port, which is exactly the point: everything
above the port is exercised, and none of it needs a device plugged in for CI to run it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from backend.common.config import RunMode
from backend.tracking.requirements import sensor_floor_thresholds
from backend.tracking.track_manager.manager import IncomingDetection, TrackManager
from backend.zones.geometry import load_site_geometry
from sensors.adapters.capabilities import SensorFloor
from sensors.adapters.conformance import PlacementContext, run_conformance
from sensors.adapters.drivers import register_builtin_drivers
from sensors.adapters.factory import Placement, build_adapters, known_drivers
from sensors.adapters.ld2451.adapter import Ld2451Adapter
from sensors.adapters.ld2451.config import Ld2451DriverConfig
from sensors.adapters.ld2451.protocol import TargetFrame, TargetReport, encode_target_frame
from sensors.adapters.ld2451.transport import FakeTransport
from sensors.adapters.registry import load_sensor_registry
from sensors.schemas.detection import AltitudeSource

EPOCH = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SITE = "bench"


@pytest.fixture(scope="module")
def geometry():
    return load_site_geometry(SITE)


@pytest.fixture(scope="module")
def registry():
    return load_sensor_registry()


def approach_frames(count: int = 400) -> list[bytes]:
    """A target closing on the sensor, one metre per frame, as the device would report it."""
    return [
        encode_target_frame(
            TargetFrame(
                alarm=True,
                targets=(
                    TargetReport(
                        angle_deg=(i // 20) % 11 - 5,
                        distance_m=max(90 - i // 4, 5),
                        approaching=True,
                        speed_kmh=25,
                        snr=70,
                    ),
                ),
            )
        )
        for i in range(count)
    ]


def bench_adapter(geometry, registry, clock, chunks) -> Ld2451Adapter:
    entry = registry["ld2451_uart"]
    placement = geometry.site.sensors[0]
    return Ld2451Adapter(
        clock=clock,
        sensor_id=placement.sensor_id,
        model=entry,
        config=Ld2451DriverConfig.model_validate(entry.driver_config),
        position_enu=geometry.site.sensor_positions_enu()[placement.sensor_id],
        frame=geometry.frame,
        boresight_deg=placement.boresight_deg,
        # Paced, so frames arrive at a rate the way a real port delivers them. Unpaced, the
        # whole script lands in one batch with near-identical timestamps and the tracker is
        # handed a situation no deployment produces.
        transport_factory=lambda baud: FakeTransport(
            chunks, description=f"fake@{baud}", pace_s=0.004
        ),
    )


# ------------------------------------------------------------------------------------------
# The factory can build it
# ------------------------------------------------------------------------------------------


def test_the_driver_is_registered_and_the_factory_can_build_it(geometry, registry) -> None:
    """The seam Phase 3 built, with something real on the other side of it for the first time."""
    register_builtin_drivers()
    assert "ld2451_uart" in known_drivers()

    placement = geometry.site.sensors[0]
    built = build_adapters(
        [
            Placement(
                sensor_id=placement.sensor_id,
                registry_key=placement.registry_key,
                position_enu=geometry.site.sensor_positions_enu()[placement.sensor_id],
                boresight_deg=placement.boresight_deg,
            )
        ],
        registry,
        clock=SimClock(EPOCH),
        frame=geometry.frame,
        run_mode=RunMode.LIVE,
    )

    assert built.live_count == 1
    assert built.simulated_count == 0
    assert built.skipped == ()
    # Constructed, not started: nothing has touched a serial port, which is what makes this
    # runnable in CI. Starting it is what would need the hardware.
    assert built.adapters[0].sensor_type.is_simulated is False


def test_a_live_site_in_simulation_mode_skips_the_real_sensor(geometry, registry) -> None:
    """The bench site has one real radar and nothing else, so SIMULATION leaves it with nothing
    — reported, not dropped."""
    placement = geometry.site.sensors[0]
    built = build_adapters(
        [
            Placement(
                sensor_id=placement.sensor_id,
                registry_key=placement.registry_key,
                position_enu=geometry.site.sensor_positions_enu()[placement.sensor_id],
            )
        ],
        registry,
        clock=SimClock(EPOCH),
        frame=geometry.frame,
        run_mode=RunMode.SIMULATION,
    )

    assert built.adapters == ()
    assert len(built.skipped) == 1
    assert "SIMULATION" in built.skipped[0].reason


# ------------------------------------------------------------------------------------------
# The conformance suite measures it
# ------------------------------------------------------------------------------------------


def test_the_live_driver_is_conformant(geometry, registry) -> None:
    """The claim ADR 0017 exists to make checkable, made against a driver that talks to a wire.

    Note what is *not* asserted: that the declared figures are true. The suite reports declared
    conformance, and the one gap it leaves — unvalidated coverage — is the flight test, not
    something software can close.
    """
    clock = SimClock(EPOCH)
    adapter = bench_adapter(geometry, registry, clock, approach_frames())
    thresholds = sensor_floor_thresholds(
        pipeline_tick_s=0.1, protected_radius_m=geometry.site.protected_radius_m
    )
    placement = geometry.site.sensors[0]

    def pump(_: int) -> None:
        clock.advance(0.1)
        time.sleep(0.006)

    report = run_conformance(
        adapter,
        thresholds=thresholds,
        clock=clock,
        cycles=60,
        pump=pump,
        placement=PlacementContext(
            geometry.site.sensor_positions_enu()[placement.sensor_id],
            geometry.frame,
            placement.boresight_deg,
        ),
    )

    assert report.conformant, report.render()
    assert report.failures == ()
    assert report.detections_observed > 0
    assert report.floor >= SensorFloor.TRACKABLE

    # The honest gap, and the only one. Everything else about this source was established.
    gaps = {gap.requirement for gap in report.gaps}
    assert gaps == {"D-3"}, report.render()


def test_the_conformance_report_names_the_link_as_unverified(geometry, registry) -> None:
    """A structural link must declare how much corruption survives its checks, and the figure is
    computed rather than asserted (ADR 0018)."""
    adapter = bench_adapter(geometry, registry, SimClock(EPOCH), approach_frames(10))
    transport = adapter.capabilities().transport

    assert transport.integrity.value == "structural"
    assert transport.residual_corruption_probability == pytest.approx(0.209, abs=0.01)


# ------------------------------------------------------------------------------------------
# The tracker forms tracks from it
# ------------------------------------------------------------------------------------------


def test_detections_off_the_wire_become_a_confirmed_track(geometry, registry) -> None:
    """The whole point, and the first time anything in this repository has tracked something
    that did not come out of the simulator.

    Reduced to the tracker's input exactly as `backend/ingestion` does, so what is exercised is
    the real path: the covariance the adapter reported is the covariance the filter uses.
    """
    import numpy as np

    from backend.events.store import SqliteEventStore

    clock = SimClock(EPOCH)
    adapter = bench_adapter(geometry, registry, clock, approach_frames())
    events = SqliteEventStore(clock)
    tracker = TrackManager(clock, events, geometry.frame)

    adapter.start()
    try:
        tracks = []
        for _ in range(60):
            clock.advance(0.1)
            time.sleep(0.006)
            incoming = []
            for detection in adapter.get_detections():
                enu = geometry.to_enu(
                    detection.position.latitude,
                    detection.position.longitude,
                    detection.position.altitude_m,
                )
                incoming.append(
                    IncomingDetection(
                        detection_id=detection.detection_id,
                        sensor_id=detection.sensor.sensor_id,
                        timestamp=detection.timestamp,
                        position_enu=np.array([enu.east_m, enu.north_m, enu.up_m]),
                        covariance=np.array(detection.measurement.position_covariance_enu),
                    )
                )
            tracks = tracker.process(incoming)
    finally:
        adapter.stop()
        events.close()

    assert tracks, "no track was formed from the radar's own detections"
    assert any(track.tracking.detection_count >= 4 for track in tracks)


def test_every_detection_off_this_device_marks_its_altitude_assumed(
    geometry, registry
) -> None:
    """The device has no elevation channel, and the marker is what stops the interface rendering
    an assumption as a measurement (ADR 0019, FM-017)."""
    clock = SimClock(EPOCH)
    adapter = bench_adapter(geometry, registry, clock, approach_frames(100))

    adapter.start()
    try:
        collected = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(collected) < 20:
            clock.advance(0.1)
            collected.extend(adapter.get_detections())
            time.sleep(0.006)
    finally:
        adapter.stop()

    assert collected
    assert all(d.position.altitude_source is AltitudeSource.ASSUMED for d in collected)
    assert all(d.measurement.position_covariance_enu[2][2] > 0.0 for d in collected)
