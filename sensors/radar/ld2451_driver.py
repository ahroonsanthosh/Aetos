"""Driver for the LD2451 testbed unit: connection management, polling, configuration, health.

This file and `ld2451_protocol.py` are the two that get replaced when the hardware is swapped.
Everything above them — `RadarSensor`, the beam geometry, the adapter, and all of Aetos — is
written against the interface, not against this unit.

## What the driver is responsible for

1. **Owning the link.** Opening the port, noticing when it dies, and reconnecting on a bounded
   backoff without anybody restarting the process. A USB adapter that falls out on a windy
   rooftop is the expected failure, not the exceptional one.
2. **Framing.** Serial reads have no relationship to frame boundaries, so bytes accumulate in a
   decoder that returns only complete frames and counts everything it throws away.
3. **Configuration.** The enable-config handshake, the tuning commands, and checking that the
   unit actually accepted them.
4. **Raw logging.** Every frame, on request, before anybody trusts a detection.
5. **Saying when it does not know.** The counters in `SensorStatus` are the difference between
   "the sky is empty" and "this link has been carrying noise for ten minutes".

## What it is not responsible for

Anything about the world. It does not know where it is mounted, what a site is, what WGS-84 is,
or what a Detection is. It reports ranges and angles in the unit's own frame and stops there;
`sensors/adapters/hardware_radar/` turns those into canonical Detections.

## Silence is not success

The single most important behaviour here: an empty target list is returned only when the unit
genuinely reported no targets. Every other outcome — port closed, port failed, frames arriving
but not decoding — is visible in `get_status()`, and polling before `connect()` raises. A driver
that returned `[]` for all of those would make a dead radar and a quiet sky identical, and the
operator would be shown the same confident empty display in both cases (FM-010).
"""

from __future__ import annotations

import math
from collections.abc import Callable

from backend.common.clock import Clock
from backend.common.config import ConfigPaths
from sensors.radar.config import DetectionTuningConfig, RadarConfig
from sensors.radar.ld2451_protocol import (
    COMMAND_REPLY_FLAG,
    MODEL_DESIGNATION,
    CommandReply,
    FrameKind,
    ProtocolError,
    disable_config_command,
    enable_config_command,
    parse_command_reply,
    parse_target_frame,
    read_firmware_version_command,
    set_sensitivity_command,
    set_target_detection_command,
)
from sensors.radar.ld2451_protocol import (
    FrameDecoder as Ld2451FrameDecoder,
)
from sensors.radar.radar_interface import (
    RadarConnectionState,
    RadarSensor,
    RadarSensorError,
    SensorStatus,
    TargetDetection,
)
from sensors.radar.raw_log import RawFrameLog
from sensors.radar.transport import PySerialTransport, SerialTransport, SerialTransportError

TransportFactory = Callable[[str, int, float], SerialTransport]
"""Builds a transport for a port, baud rate and read timeout. Injected so the driver can be
tested against a scripted byte stream, and so a future replay-from-raw-log transport needs no
change here."""

COMMAND_REPLY_TIMEOUT_S = 1.0
"""How long to wait for the unit to answer a configuration command before giving up on it."""


def _read_attempt_budget(timeout_s: float, read_timeout_s: float) -> int:
    """How many reads to allow while waiting for a reply.

    A wait bounded only by the clock is wrong here, and subtly so. Under a `SystemClock` each
    `read()` blocks for the port's own timeout and the deadline is reached in a handful of
    iterations; under a `SimClock` — which is what tests and replay use — time advances only when
    something advances it, so a reply that never arrives would spin this loop forever on a
    transport that returns immediately.

    Bounding by attempts as well as by time terminates under either clock, and the two bounds
    agree on real hardware because each attempt genuinely costs one read timeout. See ADR 0003:
    the clock being injectable is what makes this failure possible, and what makes it testable.
    """
    return max(1, math.ceil(timeout_s / read_timeout_s) + 1)


def _default_transport_factory(port: str, baud_rate: int, read_timeout_s: float) -> SerialTransport:
    return PySerialTransport(port, baud_rate, read_timeout_s)


class Ld2451Radar(RadarSensor):
    """The testbed radar unit, behind the `RadarSensor` contract.

    Construction opens nothing and can fail only on bad configuration, so building the full
    sensor set from configuration cannot fail halfway through on the one unit whose adapter is
    unplugged.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        config: RadarConfig,
        transport_factory: TransportFactory = _default_transport_factory,
    ) -> None:
        self._clock = clock
        self._config = config
        self._tuning = config.tuning
        self._transport_factory = transport_factory

        self._transport: SerialTransport | None = None
        self._decoder = Ld2451FrameDecoder()
        self._raw_log: RawFrameLog | None = None

        self._state = RadarConnectionState.DISCONNECTED
        self._reason = "not connected"
        self._baud_rate = config.link.baud_rate
        self._ever_connected = False

        self._pending: list[TargetDetection] = []
        self._last_frame_monotonic: float | None = None
        self._frames_decoded = 0
        self._targets_reported = 0
        self._malformed_payloads = 0

        self._reconnect_attempts = 0
        """Cumulative across the session. Not reset by a successful reconnect: a link that
        recovers and drops again is a flapping link, and that is exactly the diagnosis a counter
        reset on every recovery would erase."""

        self._attempts_since_loss = 0
        """Attempts since the current outage began. This is what `max_reconnect_attempts` bounds
        — the give-up decision is about one outage, not about the session's whole history."""

        self._reconnect_at: float | None = None
        self._reconnect_delay_s = config.link.reconnect_initial_delay_s

    # ----------------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------------

    def connect(self) -> None:
        if self._state is RadarConnectionState.CONNECTED:
            return

        link = self._config.link
        candidates = list(link.probe_baud_rates) if link.probe_on_connect else [self._baud_rate]

        failures: list[str] = []
        for baud_rate in candidates:
            transport = self._transport_factory(link.port, baud_rate, link.read_timeout_s)
            try:
                transport.open()
            except SerialTransportError as exc:
                failures.append(f"{baud_rate}: {exc}")
                continue

            if len(candidates) > 1 and not self._link_speaks_protocol(transport):
                transport.close()
                failures.append(f"{baud_rate}: opened but produced no decodable frame")
                continue

            self._adopt(transport, baud_rate, surface_tuning_failure=True)
            return

        self._state = RadarConnectionState.FAULTED
        self._reason = "; ".join(failures) or f"could not open {link.port}"
        raise RadarSensorError(
            f"radar {self._config.sensor_id} could not be reached on {link.port}: {self._reason}"
        )

    def _adopt(
        self, transport: SerialTransport, baud_rate: int, *, surface_tuning_failure: bool
    ) -> None:
        """Take ownership of an opened link and bring the unit up on it.

        `surface_tuning_failure` differs between the two callers because the right response
        differs. On an operator-initiated `connect()`, a unit that will not accept its tuning is
        a startup failure and must be loud — running a campaign against settings the hardware
        rejected produces data nobody can interpret. On an automatic reconnect there is no
        operator to tell, and throwing out of the poll loop would take the pipeline down over a
        radio that is already being retried; the link goes back to RECONNECTING instead and says
        why.
        """
        self._transport = transport
        self._baud_rate = baud_rate
        self._decoder.reset()
        self._state = RadarConnectionState.CONNECTED
        self._reason = f"connected on {self._config.link.port} at {baud_rate} baud"
        self._ever_connected = True
        self._attempts_since_loss = 0
        self._reconnect_at = None
        self._reconnect_delay_s = self._config.link.reconnect_initial_delay_s

        self._open_raw_log()
        try:
            self._push_tuning()
        except RadarSensorError as exc:
            if surface_tuning_failure:
                self.disconnect()
                self._state = RadarConnectionState.FAULTED
                self._reason = str(exc)
                raise
            self._begin_reconnect(f"configuration rejected: {exc}")

    def _link_speaks_protocol(self, transport: SerialTransport) -> bool:
        """Whether a candidate baud rate produces frames this protocol can decode.

        Only used while probing. The test is a firmware-version request rather than a passive
        listen: a unit in an empty room may emit nothing for seconds, and a probe that concluded
        "wrong baud rate" from silence would reject the correct one.

        A separate decoder is used so that noise from a wrong candidate never reaches the
        counters that describe the real session.
        """
        probe_decoder = Ld2451FrameDecoder()
        try:
            transport.write(read_firmware_version_command())
        except SerialTransportError:
            return False

        deadline = self._clock.monotonic() + COMMAND_REPLY_TIMEOUT_S
        attempts = _read_attempt_budget(
            COMMAND_REPLY_TIMEOUT_S, self._config.link.read_timeout_s
        )
        for _ in range(attempts):
            if self._clock.monotonic() >= deadline:
                break
            try:
                chunk = transport.read(self._config.link.max_read_bytes)
            except SerialTransportError:
                return False
            if probe_decoder.feed(chunk):
                return True
        return False

    def disconnect(self) -> None:
        """Close the link and release everything held.

        Undrained targets are dropped rather than held for a future reconnect. They describe
        where something was before the link went down, and by the time it comes back they are
        stale observations that would be timestamped as though they had just arrived.
        """
        self._state = RadarConnectionState.DISCONNECTED
        self._reason = "disconnected"
        self._reconnect_at = None

        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

        if self._raw_log is not None:
            self._raw_log.close()
            self._raw_log = None

        self._decoder.reset()
        self._pending.clear()

    # ----------------------------------------------------------------------------------------
    # Polling
    # ----------------------------------------------------------------------------------------

    def get_targets(self) -> list[TargetDetection]:
        if not self._ever_connected:
            raise RadarSensorError(
                f"radar {self._config.sensor_id} polled before connect(): a driver nobody "
                "connected reports an empty sky, which is indistinguishable from a quiet one"
            )

        if self._state is RadarConnectionState.DISCONNECTED:
            return []
        if self._state in (RadarConnectionState.RECONNECTING, RadarConnectionState.FAULTED):
            self._attempt_reconnect()
            if self._state is not RadarConnectionState.CONNECTED:
                return self._drain_pending()

        self._read_once()
        return self._drain_pending()

    def _read_once(self) -> None:
        """One non-blocking-ish read, decoded into pending targets.

        A transport failure here is a link failure, not a fatal one: the port is closed, the
        backoff starts, and the state becomes RECONNECTING rather than DISCONNECTED so that a
        yanked cable and a deliberate shutdown never look the same to the watchdog.
        """
        transport = self._transport
        if transport is None:
            return

        try:
            chunk = transport.read(self._config.link.max_read_bytes)
        except SerialTransportError as exc:
            self._begin_reconnect(str(exc))
            return

        discarded_before = self._decoder.bytes_discarded
        frames = self._decoder.feed(chunk)

        if self._raw_log is not None and self._config.raw_log.include_discarded:
            self._raw_log.record_discard_gap(self._decoder.bytes_discarded - discarded_before)

        for frame in frames:
            self._handle_frame(frame.kind, frame.payload)

    def _handle_frame(self, kind: FrameKind, payload: bytes) -> None:
        self._frames_decoded += 1
        self._last_frame_monotonic = self._clock.monotonic()
        if self._raw_log is not None:
            self._raw_log.record_frame(kind.value, payload)

        if kind is not FrameKind.DATA:
            # Configuration replies arrive interleaved with target data. Outside a configuration
            # exchange there is nothing to do with one, and counting it as a target frame would
            # be wrong; it has already been logged and counted as a frame.
            return

        try:
            reports = parse_target_frame(payload)
        except ProtocolError:
            # Framing was right and the payload layout was not — the diagnosis the counters
            # exist to make possible. Discarded whole rather than partially decoded: a payload
            # whose length disagrees with its own target count has field offsets nobody can
            # trust.
            self._malformed_payloads += 1
            return

        timestamp = self._last_frame_monotonic
        for report in reports:
            if not self._config.min_range_m <= report.distance_m <= self._config.max_range_m:
                continue
            self._pending.append(
                TargetDetection(
                    timestamp=timestamp,
                    range_m=report.distance_m,
                    azimuth_deg=report.angle_deg,
                    elevation_deg=None,
                    velocity_mps=report.radial_velocity_mps,
                    confidence=None,
                    raw_source=MODEL_DESIGNATION,
                )
            )
            self._targets_reported += 1

    def _drain_pending(self) -> list[TargetDetection]:
        targets, self._pending = self._pending, []
        return targets

    # ----------------------------------------------------------------------------------------
    # Reconnection
    # ----------------------------------------------------------------------------------------

    def _begin_reconnect(self, reason: str) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

        self._decoder.reset()
        self._state = RadarConnectionState.RECONNECTING
        self._attempts_since_loss = 0
        self._reconnect_delay_s = self._config.link.reconnect_initial_delay_s
        self._reason = f"link lost: {reason}"
        self._reconnect_at = self._clock.monotonic() + self._reconnect_delay_s

    def _attempt_reconnect(self) -> None:
        """Retry the link if the backoff has elapsed.

        Backoff doubles to the configured ceiling. Bounded rather than unbounded so that a unit
        unplugged for an hour still comes back within half a minute of being plugged in.
        """
        limit = self._config.link.max_reconnect_attempts
        if limit is not None and self._attempts_since_loss >= limit:
            self._state = RadarConnectionState.FAULTED
            self._reason = f"gave up reconnecting after {self._attempts_since_loss} attempt(s)"
            return

        if self._reconnect_at is None or self._clock.monotonic() < self._reconnect_at:
            return

        self._reconnect_attempts += 1
        self._attempts_since_loss += 1
        transport = self._transport_factory(
            self._config.link.port, self._baud_rate, self._config.link.read_timeout_s
        )
        try:
            transport.open()
        except SerialTransportError as exc:
            self._reconnect_delay_s = min(
                self._reconnect_delay_s * 2.0, self._config.link.reconnect_max_delay_s
            )
            self._reconnect_at = self._clock.monotonic() + self._reconnect_delay_s
            self._reason = f"reconnect attempt {self._attempts_since_loss} failed: {exc}"
            return

        attempts = self._attempts_since_loss
        self._adopt(transport, self._baud_rate, surface_tuning_failure=False)
        if self._state is RadarConnectionState.CONNECTED:
            self._reason = f"reconnected on {self._config.link.port} after {attempts} attempt(s)"

    # ----------------------------------------------------------------------------------------
    # Configuration
    # ----------------------------------------------------------------------------------------

    def configure(self, **settings: object) -> None:
        """Update tuning and push it to the unit.

        Unknown settings raise rather than being ignored. A typo in a calibration sweep that
        silently left the sensitivity where it was would produce a run whose recorded
        configuration and actual configuration disagree, and every conclusion drawn from that
        run would be wrong in a way nothing in the data reveals.
        """
        if not settings:
            return

        known = set(type(self._tuning).model_fields)
        unknown = sorted(set(settings) - known)
        if unknown:
            raise RadarSensorError(
                f"radar {self._config.sensor_id} has no setting(s) {unknown}; "
                f"it accepts: {sorted(known)}"
            )

        # Re-validated rather than assigned: `model_copy` does not run validators, so accepting
        # its result directly would push a sensitivity of 400 to the unit as a truncated byte.
        # Validation happens before assignment so that a rejected setting leaves the previous
        # tuning intact rather than half-applied.
        candidate = self._tuning.model_copy(update=dict(settings))
        try:
            self._tuning = type(self._tuning).model_validate(candidate.model_dump())
        except ValueError as exc:
            raise RadarSensorError(
                f"radar {self._config.sensor_id}: invalid setting: {exc}"
            ) from exc

        if self._state is RadarConnectionState.CONNECTED:
            self._push_tuning()

    @property
    def tuning(self) -> DetectionTuningConfig:
        """The tuning currently applied. Read back by test sessions so that the settings a run
        was made under are recorded with its results rather than reconstructed from memory."""
        return self._tuning

    def _push_tuning(self) -> None:
        """Send the tuning block to the unit, inside the enable/disable configuration handshake.

        Config mode is left in every path, including failure. A unit left in configuration mode
        reports no targets at all, which would turn a rejected setting into a radar that appears
        to be watching an empty sky.
        """
        transport = self._transport
        if transport is None:
            return

        try:
            self._exchange(transport, enable_config_command(), "enable configuration")
            self._exchange(
                transport,
                set_target_detection_command(
                    max_distance_m=self._tuning.max_distance_m,
                    min_speed_kmh=self._tuning.min_speed_kmh,
                    no_target_delay_s=self._tuning.no_target_delay_s,
                ),
                "set target detection parameters",
            )
            self._exchange(
                transport,
                set_sensitivity_command(
                    min_confirm_frames=self._tuning.min_confirm_frames,
                    sensitivity=self._tuning.sensitivity,
                ),
                "set sensitivity",
            )
        finally:
            try:
                self._exchange(transport, disable_config_command(), "leave configuration")
            except (RadarSensorError, SerialTransportError):
                # Already failing, or the link died mid-handshake. The reconnect path will
                # reconfigure from scratch, so nothing is gained by masking the original fault.
                pass

    def _exchange(self, transport: SerialTransport, command: bytes, description: str) -> None:
        """Send one configuration command and wait for the unit to acknowledge it.

        Waiting for the acknowledgement is the point. Writing settings and assuming they landed
        is how a calibration sweep produces a set of runs that all secretly used the same
        sensitivity.
        """
        if self._raw_log is not None:
            self._raw_log.record_command("sent", command)

        try:
            transport.write(command)
        except SerialTransportError as exc:
            raise RadarSensorError(f"could not {description}: {exc}") from exc

        expected = int.from_bytes(command[6:8], "little") | COMMAND_REPLY_FLAG
        try:
            reply = self._await_reply(transport, expected)
        except SerialTransportError as exc:
            raise RadarSensorError(f"link failed while waiting to {description}: {exc}") from exc
        if reply is None:
            raise RadarSensorError(
                f"radar {self._config.sensor_id} did not acknowledge '{description}' within "
                f"{COMMAND_REPLY_TIMEOUT_S}s"
            )
        if not reply.succeeded:
            raise RadarSensorError(
                f"radar {self._config.sensor_id} rejected '{description}' with status "
                f"0x{reply.status:04X}"
            )

    def _await_reply(self, transport: SerialTransport, command_word: int) -> CommandReply | None:
        """Read until the matching reply arrives or the timeout expires.

        Target frames encountered along the way are kept, not dropped. The unit does not stop
        observing while it is being configured, and discarding what it saw would put a gap in
        the record precisely around the moment the configuration changed — the one moment a
        calibration session most needs to see.
        """
        deadline = self._clock.monotonic() + COMMAND_REPLY_TIMEOUT_S
        attempts = _read_attempt_budget(
            COMMAND_REPLY_TIMEOUT_S, self._config.link.read_timeout_s
        )
        for _ in range(attempts):
            if self._clock.monotonic() >= deadline:
                break
            chunk = transport.read(self._config.link.max_read_bytes)
            for frame in self._decoder.feed(chunk):
                if frame.kind is FrameKind.DATA:
                    self._handle_frame(frame.kind, frame.payload)
                    continue

                self._frames_decoded += 1
                self._last_frame_monotonic = self._clock.monotonic()
                if self._raw_log is not None:
                    self._raw_log.record_command("received", frame.payload)
                try:
                    reply = parse_command_reply(frame.payload)
                except ProtocolError:
                    self._malformed_payloads += 1
                    continue
                if reply.command_word == command_word:
                    return reply
        return None

    # ----------------------------------------------------------------------------------------
    # Health
    # ----------------------------------------------------------------------------------------

    def get_status(self) -> SensorStatus:
        reason = self._reason
        if self._raw_log is not None and self._raw_log.last_error is not None:
            # A log that stopped recording halfway through a bench session must not be
            # discovered afterwards, when the session it was meant to document is over.
            reason = f"{reason}; {self._raw_log.last_error}"

        return SensorStatus(
            state=self._state,
            reason=reason,
            port=self._config.link.port if self._ever_connected else None,
            baud_rate=self._baud_rate,
            last_frame_monotonic=self._last_frame_monotonic,
            frames_decoded=self._frames_decoded,
            targets_reported=self._targets_reported,
            malformed_frames=self._decoder.malformed_frames + self._malformed_payloads,
            bytes_discarded=self._decoder.bytes_discarded,
            reconnect_attempts=self._reconnect_attempts,
        )

    def _open_raw_log(self) -> None:
        if not self._config.raw_log.enabled or self._config.raw_log.path is None:
            return
        if self._raw_log is not None:
            return
        # Resolved against the repository root rather than the working directory, for the reason
        # every other configured path in Aetos is: two processes started from different
        # directories must write the same log.
        path = ConfigPaths.default().resolve(self._config.raw_log.path)
        self._raw_log = RawFrameLog(path, self._clock)
        self._raw_log.open(
            model=MODEL_DESIGNATION,
            port=self._config.link.port,
            baud_rate=self._baud_rate,
            mount_tilt_deg=self._config.mount.tilt_deg,
        )
