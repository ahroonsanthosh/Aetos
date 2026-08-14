"""Driver behaviour for the radar testbed unit, against a scripted byte stream.

No hardware is involved and none is needed. The transport is an interface precisely so that the
cases that matter most — a link that dies mid-session, a unit that never answers, a stream that
frames as noise — are tested deterministically rather than by hoping they happen on the bench.

The behaviour under test throughout is the one this integration is shaped around: **an empty
target list means the radar saw nothing, and never anything else.** Every other outcome has to
be visible in `get_status()`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.common.clock import SimClock
from sensors.radar.config import (
    DetectionTuningConfig,
    MountConfig,
    RadarConfig,
    RawLogConfig,
    SerialLinkConfig,
)
from sensors.radar.ld2451_driver import Ld2451Radar
from sensors.radar.ld2451_protocol import (
    COMMAND_FRAME_HEADER,
    COMMAND_FRAME_TAIL,
    COMMAND_REPLY_FLAG,
    DATA_FRAME_HEADER,
    DATA_FRAME_TAIL,
)
from sensors.radar.radar_interface import RadarConnectionState, RadarSensorError
from sensors.radar.transport import SerialTransportError

EPOCH = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


def data_frame(payload: bytes) -> bytes:
    return DATA_FRAME_HEADER + len(payload).to_bytes(2, "little") + payload + DATA_FRAME_TAIL


def targets_frame(*targets: tuple[int, int, int, int, int]) -> bytes:
    body = bytes((len(targets), 0x00))
    for angle, distance, direction, speed, signal in targets:
        body += bytes((angle, distance, direction, speed, signal))
    return data_frame(body)


class ScriptedTransport:
    """A byte pipe under the test's control.

    By default it answers every configuration command with success, because the configuration
    handshake is not what most of these tests are about and a driver that could not complete it
    would never reach the behaviour they do test. Tests that care about the handshake turn
    `auto_reply` off and script the answers themselves.
    """

    def __init__(
        self,
        *,
        auto_reply: bool = True,
        reply_status: int = 0,
        fail_on_open: bool = False,
    ) -> None:
        self.auto_reply = auto_reply
        self.reply_status = reply_status
        self.fail_on_open = fail_on_open

        self.is_open = False
        self.opens = 0
        self.writes: list[bytes] = []
        self._inbox = bytearray()
        self._broken = False

    def queue(self, data: bytes) -> None:
        """Make bytes available to the next read, as the unit would have sent them."""
        self._inbox.extend(data)

    def break_link(self) -> None:
        """Pull the cable. Every subsequent read fails, as a removed USB adapter behaves."""
        self._broken = True

    def open(self) -> None:
        if self.fail_on_open:
            raise SerialTransportError("scripted open failure")
        self.is_open = True
        self.opens += 1

    def close(self) -> None:
        self.is_open = False

    def read(self, max_bytes: int) -> bytes:
        if self._broken:
            raise SerialTransportError("scripted link failure")
        chunk, self._inbox = bytes(self._inbox[:max_bytes]), bytearray(self._inbox[max_bytes:])
        return chunk

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self.auto_reply:
            command_word = int.from_bytes(data[6:8], "little")
            body = (command_word | COMMAND_REPLY_FLAG).to_bytes(2, "little")
            body += self.reply_status.to_bytes(2, "little")
            self.queue(
                COMMAND_FRAME_HEADER + len(body).to_bytes(2, "little") + body + COMMAND_FRAME_TAIL
            )
        return len(data)


def make_config(**overrides: object) -> RadarConfig:
    base: dict[str, object] = {
        "sensor_id": "bench_radar",
        "model": "test-unit",
        "description": "scripted transport, no hardware",
        "link": SerialLinkConfig(port="/dev/null", baud_rate=115_200, read_timeout_s=0.1),
        "tuning": DetectionTuningConfig(),
        "mount": MountConfig(),
        "raw_log": RawLogConfig(),
    }
    base.update(overrides)
    return RadarConfig.model_validate(base)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(EPOCH)


def connected(clock: SimClock, transport: ScriptedTransport, **config: object) -> Ld2451Radar:
    radar = Ld2451Radar(
        clock=clock, config=make_config(**config), transport_factory=lambda *_: transport
    )
    radar.connect()
    return radar


class TestLifecycle:
    def test_construction_opens_nothing(self, clock: SimClock) -> None:
        """Building the sensor set from configuration must not fail halfway through on the one
        unit whose adapter is unplugged."""
        transport = ScriptedTransport()
        Ld2451Radar(clock=clock, config=make_config(), transport_factory=lambda *_: transport)

        assert transport.opens == 0

    def test_polling_before_connect_raises_rather_than_reporting_an_empty_sky(
        self, clock: SimClock
    ) -> None:
        """The central rule. Returning [] here would disguise a wiring error as a quiet sky."""
        radar = Ld2451Radar(
            clock=clock, config=make_config(), transport_factory=lambda *_: ScriptedTransport()
        )

        with pytest.raises(RadarSensorError, match="polled before connect"):
            radar.get_targets()

    def test_connect_is_idempotent(self, clock: SimClock) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        radar.connect()

        assert transport.opens == 1

    def test_disconnect_is_idempotent_and_releases_the_port(self, clock: SimClock) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)

        radar.disconnect()
        radar.disconnect()

        assert transport.is_open is False
        assert radar.get_status().state is RadarConnectionState.DISCONNECTED

    def test_polling_after_disconnect_returns_nothing_without_raising(
        self, clock: SimClock
    ) -> None:
        """Stopping is a legitimate state a driver may be polled through — a shutdown sequence
        should not have to be ordered perfectly to avoid an exception."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        radar.disconnect()

        assert radar.get_targets() == []

    def test_a_port_that_cannot_be_opened_faults_loudly_at_connect(self, clock: SimClock) -> None:
        """A system that starts with a radar it never reached must say so at startup rather than
        three hours in."""
        radar = Ld2451Radar(
            clock=clock,
            config=make_config(),
            transport_factory=lambda *_: ScriptedTransport(fail_on_open=True),
        )

        with pytest.raises(RadarSensorError, match="could not be reached"):
            radar.connect()
        assert radar.get_status().state is RadarConnectionState.FAULTED


class TestPolling:
    def test_targets_decode_into_hardware_agnostic_reports(self, clock: SimClock) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        transport.queue(targets_frame((0x80 + 8, 42, 1, 36, 200)))

        targets = radar.get_targets()

        assert len(targets) == 1
        assert targets[0].range_m == 42.0
        assert targets[0].azimuth_deg == 8.0
        assert targets[0].velocity_mps == pytest.approx(-10.0)

    def test_elevation_is_none_because_the_unit_cannot_measure_it(self, clock: SimClock) -> None:
        """None, not zero. A zero here would be false precision that the tracker would believe,
        and the adapter turns absence into an explicit vertical uncertainty instead."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        transport.queue(targets_frame((0x80, 20, 0, 0, 100)))

        assert radar.get_targets()[0].elevation_deg is None

    def test_confidence_is_none_because_the_unit_expresses_none(self, clock: SimClock) -> None:
        """The unit reports an uncalibrated signal index. Rescaling it into 0..1 would
        manufacture a number the risk engine is entitled to treat as meaningful."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        transport.queue(targets_frame((0x80, 20, 0, 0, 250)))

        assert radar.get_targets()[0].confidence is None

    def test_an_empty_target_frame_yields_no_targets_but_proves_the_link_is_alive(
        self, clock: SimClock
    ) -> None:
        """The distinction the whole status block exists for: a radar in an empty sky still
        emits frames, so it is the frame count, not the target count, that says the link works."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        frames_before = radar.get_status().frames_decoded
        transport.queue(targets_frame())

        assert radar.get_targets() == []
        assert radar.get_status().frames_decoded > frames_before

    def test_targets_are_drained_and_not_repeated(self, clock: SimClock) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        transport.queue(targets_frame((0x80, 30, 1, 10, 150)))

        assert len(radar.get_targets()) == 1
        assert radar.get_targets() == []

    def test_returns_outside_the_configured_range_window_are_discarded(
        self, clock: SimClock
    ) -> None:
        """What the firmware is told and what we are willing to believe are separate settings."""
        transport = ScriptedTransport()
        radar = connected(clock, transport, min_range_m=5.0, max_range_m=50.0)
        transport.queue(
            targets_frame((0x80, 2, 1, 5, 90), (0x80, 30, 1, 5, 90), (0x80, 90, 1, 5, 90))
        )

        assert [t.range_m for t in radar.get_targets()] == [30.0]

    def test_a_malformed_payload_is_counted_rather_than_partially_decoded(
        self, clock: SimClock
    ) -> None:
        """Framing right, layout wrong — the diagnosis the counters exist to make possible."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        malformed_before = radar.get_status().malformed_frames
        transport.queue(data_frame(bytes((3, 0x00)) + bytes((0x80, 10, 1, 5, 90))))

        assert radar.get_targets() == []
        assert radar.get_status().malformed_frames > malformed_before

    def test_noise_that_never_frames_leaves_a_visible_signature(self, clock: SimClock) -> None:
        """The wrong-baud-rate case, which is this integration's most likely first failure. It
        must not look like a quiet sky: bytes are counted as discarded and nothing decodes."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        frames_before = radar.get_status().frames_decoded
        transport.queue(bytes(range(256)) * 20)

        assert radar.get_targets() == []
        status = radar.get_status()
        assert status.bytes_discarded > 0
        assert status.frames_decoded == frames_before

    def test_target_timestamps_come_from_the_injected_clock(self, clock: SimClock) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        clock.advance(5.0)
        transport.queue(targets_frame((0x80, 15, 1, 5, 90)))

        assert radar.get_targets()[0].timestamp == pytest.approx(clock.monotonic())


class TestReconnection:
    def test_a_dropped_link_becomes_reconnecting_not_disconnected(self, clock: SimClock) -> None:
        """A yanked cable and a deliberate shutdown must never look the same to the watchdog."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)

        transport.break_link()
        radar.get_targets()

        assert radar.get_status().state is RadarConnectionState.RECONNECTING

    def test_reconnection_waits_for_the_backoff_then_recovers(self, clock: SimClock) -> None:
        first = ScriptedTransport()
        transports = [first, ScriptedTransport()]
        radar = Ld2451Radar(
            clock=clock,
            config=make_config(
                link=SerialLinkConfig(
                    port="/dev/null", read_timeout_s=0.1, reconnect_initial_delay_s=2.0
                )
            ),
            transport_factory=lambda *_: transports.pop(0) if transports else ScriptedTransport(),
        )
        radar.connect()
        first.break_link()
        radar.get_targets()

        clock.advance(1.0)
        radar.get_targets()
        assert radar.get_status().state is RadarConnectionState.RECONNECTING

        clock.advance(2.0)
        radar.get_targets()
        assert radar.get_status().state is RadarConnectionState.CONNECTED
        assert radar.get_status().reconnect_attempts == 1

    def test_reconnection_backs_off_rather_than_hammering_a_missing_port(
        self, clock: SimClock
    ) -> None:
        """Delay doubles between failures, so a unit unplugged for an hour costs a handful of
        attempts rather than thousands."""
        first, factory = _factory_that_never_reopens()
        radar = Ld2451Radar(
            clock=clock,
            config=make_config(
                link=SerialLinkConfig(
                    port="/dev/null",
                    read_timeout_s=0.1,
                    reconnect_initial_delay_s=1.0,
                    reconnect_max_delay_s=8.0,
                )
            ),
            transport_factory=factory,
        )
        radar.connect()
        first.break_link()
        radar.get_targets()

        for _ in range(4):
            clock.advance(1.0)
            radar.get_targets()

        # Attempts at t+1 and t+3; the third is not due until t+7 because the delay doubled.
        assert radar.get_status().reconnect_attempts == 2

    def test_giving_up_faults_which_is_louder_than_reconnecting(self, clock: SimClock) -> None:
        """A port removed from the machine is not coming back on its own, and saying
        RECONNECTING forever would send an operator looking for a fault that is resolving
        itself."""
        first, factory = _factory_that_never_reopens()
        radar = Ld2451Radar(
            clock=clock,
            config=make_config(
                link=SerialLinkConfig(
                    port="/dev/null",
                    read_timeout_s=0.1,
                    reconnect_initial_delay_s=1.0,
                    max_reconnect_attempts=1,
                )
            ),
            transport_factory=factory,
        )
        radar.connect()
        first.break_link()
        radar.get_targets()

        for _ in range(6):
            clock.advance(10.0)
            radar.get_targets()

        assert radar.get_status().state is RadarConnectionState.FAULTED

    def test_a_reconnecting_link_still_reports_nothing_without_raising(
        self, clock: SimClock
    ) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        transport.break_link()
        radar.get_targets()

        assert radar.get_targets() == []


def _factory_that_never_reopens() -> tuple[ScriptedTransport, object]:
    """A working transport, plus a factory that hands it out once and then refuses to open.

    Models the case the backoff exists for: the unit was there, the link died, and the port has
    not come back — a USB adapter physically removed rather than a momentary glitch.
    """
    first = ScriptedTransport()
    state = {"first": True}

    def factory(*_: object) -> ScriptedTransport:
        if state["first"]:
            state["first"] = False
            return first
        return ScriptedTransport(fail_on_open=True)

    return first, factory


class TestConfiguration:
    def test_connecting_pushes_tuning_inside_the_enable_disable_handshake(
        self, clock: SimClock
    ) -> None:
        transport = ScriptedTransport()
        connected(clock, transport)

        words = [int.from_bytes(w[6:8], "little") for w in transport.writes]
        assert words[0] == 0x00FF, "configuration must be enabled before any setting is sent"
        assert words[-1] == 0x00FE, "configuration mode must always be left"
        assert 0x0002 in words and 0x0003 in words

    def test_configuration_mode_is_left_even_when_a_setting_is_rejected(
        self, clock: SimClock
    ) -> None:
        """A unit left in configuration mode reports no targets at all, which would turn a
        rejected setting into a radar that appears to watch an empty sky."""
        transport = ScriptedTransport(reply_status=1)
        radar = Ld2451Radar(
            clock=clock, config=make_config(), transport_factory=lambda *_: transport
        )

        with pytest.raises(RadarSensorError):
            radar.connect()

        words = [int.from_bytes(w[6:8], "little") for w in transport.writes]
        assert words[-1] == 0x00FE

    def test_a_rejected_setting_fails_connect_rather_than_running_on_unknown_tuning(
        self, clock: SimClock
    ) -> None:
        radar = Ld2451Radar(
            clock=clock,
            config=make_config(),
            transport_factory=lambda *_: ScriptedTransport(reply_status=2),
        )

        with pytest.raises(RadarSensorError, match="rejected"):
            radar.connect()

    def test_a_unit_that_never_acknowledges_is_a_failure_not_a_silent_success(
        self, clock: SimClock
    ) -> None:
        """Writing settings and assuming they landed is how a calibration sweep produces runs
        that all secretly used the same sensitivity."""
        radar = Ld2451Radar(
            clock=clock,
            config=make_config(),
            transport_factory=lambda *_: ScriptedTransport(auto_reply=False),
        )

        with pytest.raises(RadarSensorError, match="did not acknowledge"):
            radar.connect()

    def test_configure_pushes_the_new_values_to_the_unit(self, clock: SimClock) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        transport.writes.clear()

        radar.configure(sensitivity=7, min_confirm_frames=3)

        sensitivity_frames = [
            w for w in transport.writes if int.from_bytes(w[6:8], "little") == 0x0003
        ]
        assert sensitivity_frames[0][8] == 3
        assert sensitivity_frames[0][9] == 7

    def test_an_unknown_setting_raises_rather_than_being_ignored(self, clock: SimClock) -> None:
        """A typo that silently left the sensitivity where it was would produce a run whose
        recorded configuration and actual configuration disagree."""
        radar = connected(clock, ScriptedTransport())

        with pytest.raises(RadarSensorError, match="no setting"):
            radar.configure(sensitivty=7)

    def test_an_out_of_range_setting_is_rejected_before_it_reaches_the_wire(
        self, clock: SimClock
    ) -> None:
        """A sensitivity of 400 would otherwise be pushed as a truncated byte."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)

        with pytest.raises(RadarSensorError, match="invalid setting"):
            radar.configure(sensitivity=400)

    def test_a_rejected_setting_leaves_the_previous_tuning_intact(self, clock: SimClock) -> None:
        radar = connected(clock, ScriptedTransport(), tuning=DetectionTuningConfig(sensitivity=2))

        with pytest.raises(RadarSensorError):
            radar.configure(sensitivity=400)

        assert radar.tuning.sensitivity == 2

    def test_configure_before_connect_is_held_and_applied_on_connect(
        self, clock: SimClock
    ) -> None:
        """A sweep script sets tuning and then brings the link up; the settings must not be lost
        in between."""
        transport = ScriptedTransport()
        radar = Ld2451Radar(
            clock=clock, config=make_config(), transport_factory=lambda *_: transport
        )
        radar.configure(sensitivity=6)
        radar.connect()

        sensitivity_frames = [
            w for w in transport.writes if int.from_bytes(w[6:8], "little") == 0x0003
        ]
        assert sensitivity_frames[0][9] == 6

    def test_target_frames_arriving_during_configuration_are_kept(self, clock: SimClock) -> None:
        """The unit does not stop observing while it is being configured, and a gap in the record
        around the moment the configuration changed is the one gap a calibration session cannot
        afford."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        transport.queue(targets_frame((0x80, 33, 1, 20, 180)))

        radar.configure(sensitivity=5)

        assert [t.range_m for t in radar.get_targets()] == [33.0]


class TestBaudProbing:
    def test_probing_settles_on_the_rate_that_produces_a_decodable_frame(
        self, clock: SimClock
    ) -> None:
        """The documented default baud rate is contradicted between sources. Probing exists for
        that specific ambiguity, and the rate it settles on is reported so it can be written back
        into configuration rather than rediscovered every start."""
        silent = ScriptedTransport(auto_reply=False)
        speaking = ScriptedTransport()
        transports = {115_200: silent, 256_000: speaking}

        radar = Ld2451Radar(
            clock=clock,
            config=make_config(
                link=SerialLinkConfig(
                    port="/dev/null",
                    read_timeout_s=0.1,
                    probe_on_connect=True,
                    probe_baud_rates=[115_200, 256_000],
                )
            ),
            transport_factory=lambda _port, baud, _timeout: transports[baud],
        )
        radar.connect()

        assert radar.get_status().baud_rate == 256_000
        assert silent.is_open is False

    def test_probing_that_finds_nothing_faults_with_every_rate_named(
        self, clock: SimClock
    ) -> None:
        radar = Ld2451Radar(
            clock=clock,
            config=make_config(
                link=SerialLinkConfig(
                    port="/dev/null",
                    read_timeout_s=0.1,
                    probe_on_connect=True,
                    probe_baud_rates=[115_200, 256_000],
                )
            ),
            transport_factory=lambda *_: ScriptedTransport(auto_reply=False),
        )

        with pytest.raises(RadarSensorError) as failure:
            radar.connect()

        assert "115200" in str(failure.value) and "256000" in str(failure.value)

    def test_probe_noise_does_not_pollute_the_session_counters(self, clock: SimClock) -> None:
        """A wrong candidate's garbage is not evidence about the link that was eventually
        adopted."""
        noisy = ScriptedTransport(auto_reply=False)
        noisy.queue(b"\x77" * 4000)
        good = ScriptedTransport()
        transports = {115_200: noisy, 256_000: good}

        radar = Ld2451Radar(
            clock=clock,
            config=make_config(
                link=SerialLinkConfig(
                    port="/dev/null",
                    read_timeout_s=0.1,
                    probe_on_connect=True,
                    probe_baud_rates=[115_200, 256_000],
                )
            ),
            transport_factory=lambda _port, baud, _timeout: transports[baud],
        )
        radar.connect()

        assert radar.get_status().bytes_discarded == 0


class TestRawLogging:
    def test_frames_are_logged_with_a_session_header_naming_the_mount_tilt(
        self, clock: SimClock, tmp_path: Path
    ) -> None:
        """A log of returns without the tilt that produced them is uninterpretable: one fixed
        tilt is one fixed slice of sky."""
        log_path = tmp_path / "raw.log"
        transport = ScriptedTransport()
        radar = connected(
            clock,
            transport,
            mount=MountConfig(tilt_deg=25.0),
            raw_log=RawLogConfig(enabled=True, path=str(log_path)),
        )
        transport.queue(targets_frame((0x80, 40, 1, 18, 200)))
        radar.get_targets()

        lines = log_path.read_text().splitlines()
        assert lines[0].startswith("# session")
        assert "mount_tilt_deg=25.0" in lines[0]
        assert any(" data " in line for line in lines)

    def test_logging_is_off_unless_asked_for(self, clock: SimClock, tmp_path: Path) -> None:
        log_path = tmp_path / "raw.log"
        connected(clock, ScriptedTransport(), raw_log=RawLogConfig(enabled=False))

        assert not log_path.exists()

    def test_configuration_commands_and_replies_are_logged(
        self, clock: SimClock, tmp_path: Path
    ) -> None:
        """The handshake is exactly what a first-run session needs to check against the vendor
        document, byte for byte."""
        log_path = tmp_path / "raw.log"
        connected(
            clock, ScriptedTransport(), raw_log=RawLogConfig(enabled=True, path=str(log_path))
        )

        text = log_path.read_text()
        assert " sent " in text
        assert " received " in text


class TestStatus:
    def test_status_before_connect_names_no_port(self, clock: SimClock) -> None:
        radar = Ld2451Radar(
            clock=clock, config=make_config(), transport_factory=lambda *_: ScriptedTransport()
        )
        status = radar.get_status()

        assert status.state is RadarConnectionState.DISCONNECTED
        assert status.port is None
        assert status.reason

    def test_status_always_carries_a_reason(self, clock: SimClock) -> None:
        """Including when healthy. A status that only explains itself when something is wrong
        forces the layer above to invent the wording for the healthy case."""
        radar = connected(clock, ScriptedTransport())

        assert radar.get_status().reason

    def test_last_frame_time_tracks_frames_not_targets(self, clock: SimClock) -> None:
        """The liveness signal. A radar watching an empty sky still emits frames."""
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        clock.advance(3.0)
        transport.queue(targets_frame())
        radar.get_targets()

        assert radar.get_status().last_frame_monotonic == pytest.approx(clock.monotonic())

    def test_counters_distinguish_resynchronisation_from_bad_frames(
        self, clock: SimClock
    ) -> None:
        transport = ScriptedTransport()
        radar = connected(clock, transport)
        before = radar.get_status()
        transport.queue(b"\x01\x02\x03" + targets_frame((0x80, 10, 1, 5, 90)))
        radar.get_targets()

        after = radar.get_status()
        assert after.bytes_discarded == before.bytes_discarded + 3
        assert after.malformed_frames == before.malformed_frames
        assert after.targets_reported == before.targets_reported + 1
