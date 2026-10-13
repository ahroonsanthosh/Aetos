"""Bytes on a serial wire, through the whole system, to a track the API serves.

This is the test that should have existed before anyone was told live mode worked. Everything
else about the live path was covered in pieces — the parser frames, the adapter converts, the
wiring constructs — and none of that proves the pieces meet. A driver that emits valid
Detections into a pipeline that never steps them, or into an ingestion stage that rejects them
for being outside the site, is a system that starts cleanly and shows an empty map.

So: a real `Pipeline`, a real site document, the real adapter, the real ingestion, tracking,
classification, risk and projection. The only thing replaced is the serial port, which is
replaced by a script of bytes in the wire format the hardware actually produces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.common.clock import SimClock
from backend.common.config import RunMode
from backend.events.types import EventType
from backend.main import Pipeline, build_hardware_adapter, resolve_sources
from backend.system_health.models import Posture, SensorVerdict
from backend.zones.geometry import load_site_geometry
from sensors.adapters.uart_fmcw_radar.config import UartLinkConfig
from sensors.adapters.uart_fmcw_radar.link import LinkError
from sensors.adapters.uart_fmcw_radar.protocol import REPORT_HEADER, REPORT_TAIL

START = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
SITE = "hardware_testbed"


def frame(*targets: tuple[int, int, int, int, int]) -> bytes:
    """A report frame in the hardware's wire format: count, then five bytes per target."""
    payload = bytes([len(targets)]) + b"".join(bytes(t) for t in targets)
    return REPORT_HEADER + len(payload).to_bytes(2, "little") + payload + REPORT_TAIL


def approaching_target(range_m: int, azimuth_offset: int = 0) -> bytes:
    """One target closing on the sensor, on or near boresight, with a healthy quality byte."""
    return frame((0x80 + azimuth_offset, range_m, 1, 20, 120))


class ScriptedLink:
    """A byte pipe that plays a prepared script, one chunk per read.

    `vanish()` models an unplugged USB adapter rather than a closed handle: the port stops
    opening at all. Merely closing it would let the adapter's reconnect succeed on the next
    cycle, which is not what a device that is no longer there does.
    """

    def __init__(self, config: UartLinkConfig) -> None:
        self.config = config
        self.is_open = False
        self.gone = False
        self.chunks: list[bytes] = []
        self.written: list[bytes] = []

    def vanish(self) -> None:
        self.gone = True
        self.is_open = False

    def open(self) -> None:
        if self.gone:
            raise LinkError(f"no such device: {self.config.port}")
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read(self, max_bytes: int) -> bytes:
        if self.gone or not self.is_open:
            # What a real port does when the device is unplugged: it raises. Returning b""
            # would make a vanished sensor indistinguishable from a quiet one, which is the
            # confusion the whole health path exists to prevent.
            raise LinkError(f"device disappeared: {self.config.port}")
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)


@pytest.fixture
def live_system(tmp_path: Path) -> tuple[Pipeline, ScriptedLink, SimClock]:
    """A full pipeline against the shipped hardware site, with a scripted link in place of
    the serial port."""
    clock = SimClock(START)
    geometry = load_site_geometry(SITE)
    pipeline = Pipeline(clock, geometry, tmp_path)

    links: list[ScriptedLink] = []

    def factory(config: UartLinkConfig) -> ScriptedLink:
        link = ScriptedLink(config)
        links.append(link)
        return link

    pipeline.attach_hardware(link_factory=factory)
    assert len(links) == 1, "the shipped hardware site should fit exactly one radar"

    yield pipeline, links[0], clock

    pipeline.close()


# -- the whole path ----------------------------------------------------------------------


def test_a_report_frame_becomes_a_track(live_system) -> None:
    """The claim live mode rests on: bytes in, a track an operator can be shown out.

    Deliberately unmocked between the wire and the projection. If ingestion starts rejecting
    these detections — for being outside the site, for a timestamp it dislikes, for a schema
    change — this fails, and that is the point.
    """
    pipeline, link, clock = live_system

    # A target closing steadily on the sensor over several cycles, which is what the tracker
    # needs to confirm anything: one detection is a return, several in sequence are a track.
    for step, range_m in enumerate((90, 80, 70, 60, 50, 40)):
        link.chunks.append(approaching_target(range_m))
        accepted = pipeline.step()
        assert accepted == 1, f"cycle {step}: the detection was not accepted by ingestion"
        clock.advance(0.5)

    pipeline.projection.catch_up()
    tracks = pipeline.repositories.tracks.list_all()

    assert tracks, "six accepted detections on a closing target produced no track at all"


def test_the_track_is_positioned_where_the_sensor_pointed(live_system) -> None:
    """A track 50 m due north of a north-facing sensor, not somewhere else entirely.

    Catches the whole class of geometry transcription bugs — boresight applied backwards, the
    altitude datum missed, latitude and longitude swapped — which produce a system that runs
    perfectly and draws the target in the wrong place.
    """
    pipeline, link, clock = live_system
    site = pipeline.geometry.site
    placement = site.sensors[0]
    assert placement.boresight_deg == 0.0, "this test reads the shipped north-facing default"

    for range_m in (60, 55, 50):
        link.chunks.append(approaching_target(range_m))
        pipeline.step()
        clock.advance(0.5)

    pipeline.projection.catch_up()
    track = pipeline.repositories.tracks.list_all()[0].track

    # North of the sensor: latitude above it, longitude essentially unchanged, and the
    # distance from the sensor is what the sensor said rather than a multiple of it.
    assert track.position.latitude > placement.position.latitude
    assert track.position.longitude == pytest.approx(placement.position.longitude, abs=1e-4)
    assert track.enu.north_m == pytest.approx(50.0, abs=15.0), (
        "a 50 m return on boresight must land ~50 m north of the sensor, not at some "
        "transposed or doubled distance"
    )
    assert abs(track.enu.east_m) < 15.0


def test_detections_survive_ingestion_validation(live_system) -> None:
    """Ingestion is the stage most able to reject live data silently.

    It validates the schema, deduplicates, and normalises into the site frame. A detection at
    100 m from a site whose zones extend to 300 m must pass all three, and if a future change
    to any of them starts dropping real returns, the failure belongs here rather than in a
    puzzled operator's lap.
    """
    pipeline, link, clock = live_system

    link.chunks.append(approaching_target(100))
    assert pipeline.step() == 1

    clock.advance(0.5)
    link.chunks.append(approaching_target(10))
    assert pipeline.step() == 1, "a target at the configured minimum range must be accepted"


def test_targets_outside_the_configured_window_never_reach_ingestion(live_system) -> None:
    """The gate is the adapter's, not ingestion's — a 200 m report from a 100 m-rated sensor
    is discarded before it becomes a Detection at all."""
    pipeline, link, clock = live_system

    link.chunks.append(approaching_target(200))

    assert pipeline.step() == 0


def test_several_targets_in_one_frame_all_arrive(live_system) -> None:
    """The sensor reports up to several targets per frame; each is its own Detection."""
    pipeline, link, _ = live_system

    link.chunks.append(
        frame((0x80, 50, 1, 20, 120), (0x80 + 10, 70, 0, 15, 100), (0x80 - 10, 30, 1, 25, 110))
    )

    assert pipeline.step() == 3


def test_noise_on_the_wire_produces_no_detections_and_no_crash(live_system) -> None:
    """A wrong baud rate delivers bytes that never frame. The pipeline must survive a cycle of
    pure rubbish without raising — the alternative is a dead pipeline task and a frozen
    display, which looks exactly like a quiet sky."""
    pipeline, link, _ = live_system

    link.chunks.append(bytes(range(256)) * 4)

    assert pipeline.step() == 0
    assert pipeline.sensor_health()[0].state.value == "degraded"


def test_a_dead_link_mid_run_does_not_stop_the_pipeline(live_system) -> None:
    """Unplugging the USB adapter must cost detections, not the run."""
    pipeline, link, clock = live_system

    link.chunks.append(approaching_target(50))
    assert pipeline.step() == 1

    link.vanish()  # as though the USB adapter were pulled out
    clock.advance(0.5)

    assert pipeline.step() == 0
    assert pipeline.sensor_health()[0].state.value in {"offline", "degraded"}


def test_the_raw_log_captures_the_session(live_system, tmp_path: Path) -> None:
    """The shipped site configures raw logging, and it must actually produce a file — it is the
    only record that can explain a session afterwards."""
    pipeline, link, _ = live_system

    link.chunks.append(approaching_target(50))
    pipeline.step()

    log = tmp_path / "raw" / "radar_testbed_01.log"
    assert log.exists(), "the shipped raw_log_path produced no file"
    assert approaching_target(50).hex() in log.read_text()


def test_a_sensor_that_comes_back_starts_reporting_again(live_system) -> None:
    """Replugging the USB adapter must recover without restarting the process.

    A driver that needed a restart to recover would mean an operator loses their whole picture
    — every track, every open alert — because a cable was nudged.
    """
    pipeline, link, clock = live_system

    link.vanish()
    clock.advance(0.5)
    assert pipeline.step() == 0

    # Plugged back in, and past the reconnect backoff.
    link.gone = False
    clock.advance(5.0)
    link.chunks.append(approaching_target(40))

    assert pipeline.step() == 1, "the adapter did not reacquire a port that came back"
    assert pipeline.sensor_health()[0].state.value == "ok"


def test_a_long_quiet_run_neither_grows_nor_falls_over(live_system) -> None:
    """Two hundred cycles of nothing, which is what a bench radar does most of the time.

    Guards the boring failure: a buffer that grows on every empty read, a counter that
    overflows, an exception on the thousandth cycle that kills the pipeline task and freezes
    the display while the server carries on answering.
    """
    pipeline, link, clock = live_system

    for _ in range(200):
        assert pipeline.step() == 0
        clock.advance(0.5)

    link.chunks.append(approaching_target(50))
    assert pipeline.step() == 1, "the adapter stopped working after a long quiet period"


def test_interleaved_noise_and_valid_frames_still_yield_detections(live_system) -> None:
    """A marginal link delivers both. Losing the good frames because bad ones arrived beside
    them would make a noisy environment look like an empty one."""
    pipeline, link, clock = live_system

    link.chunks.append(b"\x00\x11\x22" + approaching_target(60) + b"\xff\xfe")
    assert pipeline.step() == 1

    clock.advance(0.5)
    link.chunks.append(approaching_target(55) + bytes(range(32)))
    assert pipeline.step() == 1


# -- the run mode the operator asks for ----------------------------------------------------


def test_the_shipped_hardware_site_satisfies_live_mode() -> None:
    """`./start.sh --live` with no arguments must not be refused by its own default site.

    The shipped site and the shipped default are two files that have to agree, and nothing else
    checks that they do.
    """
    site = load_site_geometry(SITE).site

    attach_hardware, scenario = resolve_sources(RunMode.LIVE, site, None)

    assert attach_hardware is True
    assert scenario is None


def test_the_shipped_hardware_site_is_refused_in_simulation_with_a_scenario() -> None:
    """The converse, and the reason `--live` switches the site: the hardware site has no
    simulated sensors, so a scenario there would drive nothing."""
    from backend.main import ModeRefused

    site = load_site_geometry(SITE).site

    with pytest.raises(ModeRefused):
        resolve_sources(RunMode.SIMULATION, site, "a_benign_transit")


# --------------------------------------------------------------------------------------
# One sensor's failure is not every sensor's failure
# --------------------------------------------------------------------------------------


class _ExplodingLink(ScriptedLink):
    """A link whose reads raise something the driver does not expect.

    Not a `LinkError` — the driver handles those. This stands in for a genuine defect in a
    driver, which is the case the pipeline has to survive without taking the site down.
    """

    def read(self, max_bytes: int) -> bytes:
        raise RuntimeError("driver defect")


def test_one_failing_sensor_does_not_cost_the_others_their_cycle(tmp_path: Path) -> None:
    """Start-up already tolerates a broken sensor; the running cycle must agree with it.

    Before this, `step()` polled adapters in a bare loop, so one driver raising cost *every*
    sensor its cycle. The run survived — the task-level guard caught it — but the operator
    picture stopped advancing on every sensor because one was faulty.
    """
    clock = SimClock(START)
    geometry = load_site_geometry(SITE)
    pipeline = Pipeline(clock, geometry, tmp_path)

    healthy: list[ScriptedLink] = []

    def factory(config: UartLinkConfig) -> ScriptedLink:
        link = ScriptedLink(config)
        healthy.append(link)
        return link

    try:
        pipeline.attach_hardware(link_factory=factory)
        # A second adapter on the same pipeline, whose driver is broken.
        broken = build_hardware_adapter(
            clock,
            geometry.site.sensors[0].model_copy(update={"sensor_id": "radar_broken"}),
            tmp_path,
            geometry.site.reference.altitude_m,
            link_factory=_ExplodingLink,
        )
        broken.start()
        pipeline._attach([broken])  # noqa: SLF001 — attaching a second adapter is the setup

        healthy[0].chunks.append(approaching_target(50))

        assert pipeline.step() == 1, "the healthy sensor's detection was lost to the broken one"

        states = {h.sensor_id: h.state.value for h in pipeline.sensor_health()}
        assert states["radar_testbed_01"] == "ok"
        assert states["radar_broken"] == "degraded", (
            "a driver that raised must not be able to report itself healthy"
        )
    finally:
        pipeline.close()


def test_a_sensor_that_recovers_stops_being_reported_as_degraded(tmp_path: Path) -> None:
    """Otherwise a single transient fault marks a sensor bad for the life of the process."""
    clock = SimClock(START)
    geometry = load_site_geometry(SITE)
    pipeline = Pipeline(clock, geometry, tmp_path)

    class Flaky(ScriptedLink):
        fail = True

        def read(self, max_bytes: int) -> bytes:
            if Flaky.fail:
                raise RuntimeError("driver defect")
            return super().read(max_bytes)

    links: list[Flaky] = []

    def factory(config: UartLinkConfig) -> Flaky:
        link = Flaky(config)
        links.append(link)
        return link

    try:
        pipeline.attach_hardware(link_factory=factory)
        pipeline.step()
        assert pipeline.sensor_health()[0].state.value == "degraded"

        Flaky.fail = False
        links[0].chunks.append(approaching_target(50))
        clock.advance(0.5)

        assert pipeline.step() == 1
        assert pipeline.sensor_health()[0].state.value == "ok"
    finally:
        Flaky.fail = True
        pipeline.close()


# -- the watchdog, over a real cycle ---------------------------------------------------------


def test_the_watchdog_tells_a_dead_link_from_a_quiet_sky(live_system) -> None:
    """FM-010, end to end, and the reason this module exists.

    Two runs of the same length with the same output — no detections — and the watchdog must
    reach different conclusions, because the *evidence* differs: one sensor is in contact and
    seeing nothing, the other cannot be reached at all. Anything that reported these the same
    way would be exactly the green light this system is written against.
    """
    pipeline, link, clock = live_system
    threshold = pipeline.watchdog.policy.threshold_for(pipeline.adapters[0].sensor_type)

    # A quiet sky: frames keep arriving, and every one of them reports zero targets. That is
    # what this radar does over an empty sky — it does not stop transmitting — which is exactly
    # why the adapter can be sure it is still in contact while having nothing to report.
    # Past the startup grace window as well as the silence threshold — the two are separate
    # numbers and a run shorter than both would prove nothing about either.
    grace = pipeline.watchdog.policy.startup_grace_s
    while (clock.now() - START).total_seconds() <= max(threshold, grace) + 1.0:
        link.chunks.append(frame())
        pipeline.step()
        clock.advance(threshold / 4)

    link.chunks.append(frame())
    pipeline.step()

    quiet = pipeline.watchdog.report()
    assert [s.verdict for s in quiet.sensors] == [SensorVerdict.QUIET]
    assert quiet.posture is Posture.NOMINAL, "an empty sky is not a fault"

    # The same silence, with the adapter unplugged underneath it.
    link.vanish()
    pipeline.step()

    dead = pipeline.watchdog.report()
    assert [s.verdict for s in dead.sensors] == [SensorVerdict.OFFLINE]
    assert dead.posture is Posture.BLIND


def test_detections_keep_the_posture_nominal(live_system) -> None:
    """The converse. A sensor producing data must never drift into a quiet verdict."""
    pipeline, link, clock = live_system

    for range_m in (90, 80, 70, 60):
        link.chunks.append(approaching_target(range_m))
        pipeline.step()
        clock.advance(0.5)

    report = pipeline.watchdog.report()
    assert [s.verdict for s in report.sensors] == [SensorVerdict.OK]
    assert report.posture is Posture.NOMINAL


def test_a_pipeline_that_stops_stepping_is_visible_as_stalled(live_system) -> None:
    """The self-test, against a real pipeline rather than a stub.

    Nothing inside the pipeline can report this: a cycle that is not running executes no code
    that could raise. The posture ages out on its own because it is evaluated against the clock
    at the moment somebody asks, which is the only place the check survives.
    """
    pipeline, link, clock = live_system
    link.chunks.append(approaching_target(50))
    pipeline.step()
    assert pipeline.watchdog.report().posture is Posture.NOMINAL

    clock.advance(pipeline.watchdog.policy.stall_threshold_s + 1.0)

    assert pipeline.watchdog.report().posture is Posture.STALLED


def test_losing_and_regaining_a_sensor_is_recorded_in_the_log(live_system) -> None:
    """An operator reconstructing the incident afterwards reads the log, not the display."""
    pipeline, link, clock = live_system
    link.chunks.append(approaching_target(50))
    pipeline.step()
    clock.advance(0.5)

    link.vanish()
    pipeline.step()
    clock.advance(0.5)

    # Plugged back in, and past the adapter's reconnect backoff.
    link.gone = False
    clock.advance(5.0)
    link.chunks.append(approaching_target(50))
    assert pipeline.step() == 1, "the adapter did not reacquire a port that came back"

    recorded = [
        e.event_type
        for e in pipeline.events.read_from(1)
        if e.event_type in {EventType.DEGRADED_MODE_ENTERED, EventType.DEGRADED_MODE_EXITED}
    ]
    assert recorded == [EventType.DEGRADED_MODE_ENTERED, EventType.DEGRADED_MODE_EXITED]
