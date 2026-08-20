"""A physically fitted UART radar, as a source of canonical Detections.

This is the first adapter in Aetos that reads a real device. Everything the device is peculiar
about stops here, exactly as `sensors/adapters/README.md` says it must: the framing lives in
`protocol.py`, the link in `transport.py`, the mounting and error figures in `config.py`, and
what leaves this module is a `Detection` indistinguishable in shape from a simulated one. The
sole channel that carries real-versus-simulated is `SensorRef.sensor_type`, which is `radar`
rather than `simulated_radar`, and that is what `GET /system/mode` counts.

## What this device measures, and what it does not

It reports, per target: azimuth relative to boresight, slant range to the metre, a direction
flag, a speed to the km/h, and a unitless signal level. That is all. In particular:

**There is no elevation.** Not a coarse one, not an ambiguous one — none. A single azimuth and
range describe a circle's worth of possible positions, and the adapter has to place the
detection somewhere. It places it at the centre of the beam, at the configured mount tilt, and
gives it a vertical uncertainty derived from the beam's full width, so the filter is told in
the covariance exactly how little the vertical is known. Reporting a confident altitude here
would be the FM-004 failure in its purest form: a number that looks like a measurement and is
an assumption.

**There is no classification.** The device's firmware is tuned for vehicles, and a target it
reports is a moving reflector, not an identified one. This adapter therefore emits no
`ClassificationHint` at all rather than asserting `VEHICLE` — that label would be evidence the
classification module weights, and it would be evidence for something the device never
determined. Silence is the truthful output.

**The range figure is rated for vehicle-scale returns.** A small airframe presents a far
smaller radar cross-section, so the range at which this device sees one is unknown and is
almost certainly a fraction of the rated hundred metres. Nothing here compensates for that;
the coverage numbers in configuration are a gate, not a promise, and the bench procedure in
`docs/hardware_radar_bench.md` is how a site learns what its own detection range actually is.

## Threads

The port is read by a background thread, because a serial read must not stall the pipeline's
cycle and the device frames on its own cadence rather than ours. The thread does the framing
and decoding and hands finished reports across a bounded queue; `get_detections()` drains that
queue on the caller's thread and is the only place identifiers are assigned, which keeps the
identifier generator single-threaded as ADR 0006 requires. The queue is bounded and drops the
oldest report when it overflows, counting every drop — an unbounded queue would turn a stalled
pipeline into a memory leak, and a silent drop would turn it into a lie.
"""

from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass
from datetime import datetime

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.adapters.radar.config import RadarHardwareConfig
from sensors.adapters.radar.protocol import (
    FrameDecoder,
    FrameKind,
    ProtocolError,
    TargetReport,
    decode_target_frame,
)
from sensors.adapters.radar.transport import ByteTransport, TransportError
from sensors.schemas.detection import (
    Detection,
    GeoPosition,
    Kinematics,
    Measurement,
    Provenance,
    Quality,
    SensorRef,
)

QUEUE_DEPTH_REPORTS = 4096
"""Reports the hand-off queue holds before the oldest is dropped. Roughly seven minutes at ten
reports a second, which is far longer than any pipeline stall that is not already an outage."""

DESYNC_BYTES_DEGRADED = 256
"""Discarded bytes, since start, past which the link is reported degraded rather than healthy.

A link at the wrong baud rate discards continuously; a link that was joined mid-frame discards
a few bytes once. The threshold separates the two without needing a rate calculation."""


@dataclass(frozen=True, slots=True)
class _Report:
    """One decoded target with the time its frame completed."""

    timestamp: datetime
    target: TargetReport


class UartRadarAdapter(SensorAdapter):
    """One fitted radar at one place on one site.

    Construction is cheap and opens nothing; `start()` acquires the port and starts the reader
    thread, per the lifecycle in `SensorAdapter` — building the adapter set from configuration
    must not half-fail on a device that happens to be unplugged.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        sensor_id: str,
        config: RadarHardwareConfig,
        transport: ByteTransport,
        position_enu: EnuPoint,
        frame: LocalFrame,
        boresight_deg: float = 0.0,
    ) -> None:
        super().__init__(clock, sensor_id, config.sensor_type)
        self._config = config
        self._transport = transport
        self._position = position_enu
        self._frame = frame
        self._boresight_deg = boresight_deg
        self._tilt_rad = math.radians(config.tilt_deg)

        self._ids = detection_id_generator(sensor_id)
        self._decoder = FrameDecoder()
        self._queue: queue.Queue[_Report] = queue.Queue(maxsize=QUEUE_DEPTH_REPORTS)

        self._reader: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()

        self._running = False
        self._ever_started = False
        self._detections_emitted = 0
        self._last_detection_time: datetime | None = None

        # Written by the reader thread, read by health() on the caller's. Guarded by _lock.
        self._last_bytes_monotonic: float | None = None
        self._transport_error: str | None = None
        self._dropped_reports = 0
        self._rejected_reports = 0

    @property
    def config(self) -> RadarHardwareConfig:
        return self._config

    def start(self) -> None:
        if self._running:
            return

        self._transport.open()  # TransportError propagates: a port that will not open is not
        # a quiet sky, and starting anyway would present it as one.

        self._stopping.clear()
        with self._lock:
            self._transport_error = None
            self._last_bytes_monotonic = self._clock.monotonic()
            self._dropped_reports = 0
            self._rejected_reports = 0
        self._detections_emitted = 0
        self._last_detection_time = None
        self._decoder = FrameDecoder()

        self._running = True
        self._ever_started = True
        self._reader = threading.Thread(
            target=self._read_loop, name=f"radar-{self.sensor_id}", daemon=True
        )
        self._reader.start()

    def stop(self) -> None:
        if not self._running:
            self._transport.close()
            return

        self._running = False
        self._stopping.set()

        reader, self._reader = self._reader, None
        if reader is not None:
            # Bounded by the transport's read timeout, which is why that timeout is part of
            # the transport contract rather than a setting somebody might raise to a minute.
            reader.join(timeout=5.0)

        self._transport.close()

    def get_detections(self) -> list[Detection]:
        if not self._ever_started:
            raise SensorAdapterError(
                f"sensor {self.sensor_id} polled before start(): an adapter nobody started "
                "reports an empty sky, which is indistinguishable from a quiet one"
            )
        if not self._running:
            return []

        detections = [
            detection
            for report in self._drain()
            if (detection := self._to_detection(report)) is not None
        ]

        if detections:
            self._detections_emitted += len(detections)
            self._last_detection_time = detections[-1].timestamp
        return detections

    def health(self) -> SensorHealth:
        state, reason = self._assess()
        return SensorHealth(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            state=state,
            last_detection_time=self._last_detection_time,
            detections_emitted=self._detections_emitted,
            reason=reason,
        )

    # -- reader thread ------------------------------------------------------------------

    def _read_loop(self) -> None:
        """Read, frame, decode, hand off. The only thread that touches the transport."""
        while not self._stopping.is_set():
            try:
                data = self._transport.read()
            except TransportError as exc:
                # A failed link stays failed until someone restarts the adapter. Retrying in
                # a tight loop here would bury the fault under a log flood and leave health()
                # flickering between states it cannot explain.
                with self._lock:
                    self._transport_error = str(exc)
                return

            if not data:
                continue

            timestamp = self._clock.now()
            with self._lock:
                self._last_bytes_monotonic = self._clock.monotonic()

            for frame in self._decoder.feed(data):
                if frame.kind is not FrameKind.TARGET_DATA:
                    # Command replies are the device answering a configuration exchange this
                    # adapter never starts. Reading past them keeps the stream in sync when a
                    # bench tool has been talking to the device on the same port.
                    continue
                self._hand_off(frame.payload, timestamp)

    def _hand_off(self, payload: bytes, timestamp: datetime) -> None:
        try:
            decoded = decode_target_frame(
                payload, approaching_code=self._config.protocol.approaching_code
            )
        except ProtocolError:
            self._decoder.stats.malformed_frames += 1
            return

        for target in decoded.targets:
            report = _Report(timestamp=timestamp, target=target)
            try:
                self._queue.put_nowait(report)
            except queue.Full:
                # Drop the oldest, not the newest: if the pipeline has fallen behind, the
                # freshest picture is the one worth keeping. Counted either way.
                with self._lock:
                    self._dropped_reports += 1
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(report)
                except (queue.Empty, queue.Full):  # pragma: no cover - a racing drain
                    pass

    # -- conversion ---------------------------------------------------------------------

    def _drain(self) -> list[_Report]:
        reports: list[_Report] = []
        while True:
            try:
                reports.append(self._queue.get_nowait())
            except queue.Empty:
                return reports

    def _to_detection(self, report: _Report) -> Detection | None:
        target = report.target
        coverage = self._config.coverage

        if not coverage.min_range_m <= target.range_m <= coverage.max_range_m:
            with self._lock:
                self._rejected_reports += 1
            return None
        if abs(math.radians(target.azimuth_deg)) > self._config.half_azimuth_rad:
            with self._lock:
                self._rejected_reports += 1
            return None

        bearing_rad = math.radians(
            self._boresight_deg + self._config.protocol.azimuth_sign * target.azimuth_deg
        )
        horizontal_m = target.range_m * math.cos(self._tilt_rad)
        up_m = self._position.up_m + target.range_m * math.sin(self._tilt_rad)

        point = EnuPoint(
            east_m=self._position.east_m + horizontal_m * math.sin(bearing_rad),
            north_m=self._position.north_m + horizontal_m * math.cos(bearing_rad),
            up_m=up_m,
        )
        geodetic: GeodeticPoint = self._frame.to_geodetic(point)

        covariance = self._covariance(bearing_rad, target.range_m)
        variances = [covariance[i][i] for i in range(3)]

        return Detection(
            detection_id=self._ids.next(),
            timestamp=report.timestamp,
            sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
            position=GeoPosition(
                latitude=geodetic.latitude_deg,
                longitude=geodetic.longitude_deg,
                altitude_m=geodetic.altitude_m,
            ),
            measurement=Measurement(
                # The isotropic figure is the root-mean-square of the three axes and exists
                # only for consumers that cannot read a covariance. It is a poor summary of an
                # error this anisotropic — vertical dominates it by an order of magnitude —
                # and the covariance below is the authoritative statement.
                position_uncertainty_m=math.sqrt(sum(variances) / 3.0),
                position_covariance_enu=covariance,
            ),
            quality=Quality(
                confidence=self._config.quality.confidence_for(target.signal_level),
                # snr_db stays None on purpose. The device reports a unitless level, and
                # writing it into a field named in decibels would be a unit lie that no
                # consumer downstream could detect.
                snr_db=None,
            ),
            kinematics=Kinematics(
                radial_velocity_mps=target.radial_velocity_mps,
                provenance=Provenance.MEASURED,
            ),
            # No classification hint: see the module docstring. The device detects motion, it
            # does not identify what is moving.
            classification=None,
        )

    def _covariance(self, bearing_rad: float, range_m: float) -> list[list[float]]:
        """Position covariance in local ENU metres squared, from the sensor's error geometry.

        Three independent terms, in the sensor's own frame, rotated into ENU:

        * **along the line of sight** — the range error, small and roughly constant;
        * **across it** — the bearing error, which subtends more metres the further away the
          target is, so it scales with range;
        * **vertical** — not a measurement error at all but the *absence* of a measurement.
          The target is somewhere in a beam of known width and unknown occupancy; treating
          that as uniform over the beam gives a standard deviation of `range * beamwidth /
          sqrt(12)`, which at 100 m and a 20 degree beam is 10 m. That number belongs in the
          covariance rather than in a comment, because the filter reads the covariance.
        """
        sigma_range = self._config.uncertainty.range_sigma_m
        sigma_cross = range_m * math.radians(self._config.uncertainty.azimuth_sigma_deg)
        sigma_up = (
            range_m * math.radians(self._config.coverage.elevation_beamwidth_deg) / math.sqrt(12.0)
        )

        # Unit vectors in the horizontal plane: along the line of sight, and across it.
        along_e, along_n = math.sin(bearing_rad), math.cos(bearing_rad)
        cross_e, cross_n = math.cos(bearing_rad), -math.sin(bearing_rad)

        var_r, var_c = sigma_range**2, sigma_cross**2
        east_east = var_r * along_e**2 + var_c * cross_e**2
        north_north = var_r * along_n**2 + var_c * cross_n**2
        east_north = var_r * along_e * along_n + var_c * cross_e * cross_n

        return [
            [east_east, east_north, 0.0],
            [east_north, north_north, 0.0],
            [0.0, 0.0, max(sigma_up**2, 1e-6)],
        ]

    # -- health -------------------------------------------------------------------------

    def _assess(self) -> tuple[SensorState, str]:
        if not self._ever_started:
            return SensorState.NOT_STARTED, "constructed but not started"
        if not self._running:
            return SensorState.OFFLINE, "stopped"

        with self._lock:
            error = self._transport_error
            last_bytes = self._last_bytes_monotonic
            dropped = self._dropped_reports
            rejected = self._rejected_reports

        if error is not None:
            return SensorState.OFFLINE, f"link failed: {error}"

        # Explicitly against None, not truthiness: a monotonic clock legitimately reads 0.0,
        # and treating that as "unknown" would report a link healthy for as long as the
        # process had been running.
        reference = last_bytes if last_bytes is not None else self._clock.monotonic()
        silence_s = self._clock.monotonic() - reference
        if silence_s > self._config.link.silence_timeout_s:
            # Deliberately not "no targets": the device reports on its cadence even when it
            # sees nothing, so silence on the wire is the link, not the sky (FM-010).
            return SensorState.DEGRADED, (
                f"no data on {self._config.link.port} for {silence_s:.0f}s; the device reports "
                "even when it sees nothing, so this is the link rather than an empty sky"
            )

        discarded = self._decoder.stats.bytes_discarded
        if discarded > DESYNC_BYTES_DEGRADED:
            return SensorState.DEGRADED, (
                f"{discarded} bytes discarded resynchronising; check the baud rate and the "
                "TX/RX wiring"
            )
        if self._decoder.stats.malformed_frames:
            return SensorState.DEGRADED, (
                f"{self._decoder.stats.malformed_frames} malformed target frames since start"
            )
        if dropped:
            return SensorState.DEGRADED, (
                f"{dropped} reports dropped: the pipeline is not draining this sensor fast "
                "enough"
            )
        if rejected:
            return SensorState.OK, (
                f"nominal; {rejected} reports rejected as outside the configured coverage"
            )
        return SensorState.OK, "nominal"
