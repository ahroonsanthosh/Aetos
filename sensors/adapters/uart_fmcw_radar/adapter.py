"""`UartFmcwRadarAdapter` — the `SensorAdapter` implementation for the testbed radar.

Ties three things together: the wire protocol (`protocol.py`, pure), the serial link
(`transport.py`, the only I/O), and the mount's geometry and calibration placeholders
(`config.RadarMountConfig`) — into canonical `Detection` objects. Nothing downstream of
`sensors.adapters.interface.SensorAdapter` can tell this apart from the simulator or from any
other real sensor that will eventually replace it (ADR 0002).

Read this package's README before relying on this in anything operational: several fields this
adapter emits are honest placeholders, not measured facts, because the underlying module does
not measure what they describe (elevation) or because this deployment has not yet been
calibrated (range/angle uncertainty).
"""

from __future__ import annotations

import math
from datetime import datetime

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.adapters.uart_fmcw_radar.config import RadarMountConfig, SerialLinkConfig
from sensors.adapters.uart_fmcw_radar.protocol import (
    FrameError,
    RawTarget,
    extract_data_frames,
    parse_targets,
)
from sensors.adapters.uart_fmcw_radar.transport import SerialTransport, SerialTransportError
from sensors.schemas.detection import (
    Detection,
    GeoPosition,
    Kinematics,
    Measurement,
    Provenance,
    Quality,
    SensorRef,
    SensorType,
)

_MIN_EVENTS_FOR_ERROR_RATE = 20
"""Below this many framing events since start, a run of failures is as likely to be start-up
noise (opening the port mid-stream, so the first bytes are the tail of someone else's frame) as
a marginal link, so DEGRADED is not reported until there is enough of a sample to mean
something."""

_DEGRADED_FRAME_ERROR_RATE = 0.2
"""Above this fraction of framing events failing, report DEGRADED rather than OK.

A "failure" is a resync (a header that led nowhere) or a payload that framed but did not parse.
Resyncs are counted because they are the *only* observable symptom of a marginal link: a link
corrupting bytes mid-frame shows up as footers that do not match, not as payloads that fail to
parse. Counting parse failures alone would leave this state effectively unreachable — a health
signal that cannot fire is worse than none, because it implies coverage that does not exist.

The adapter reports this because it is the adapter's own knowledge of its link, not a judgement
about whether the sensor is broken versus looking at an empty scene — that distinction is
`backend/system_health`'s job (see `sensors/adapters/README.md`, "Health")."""


class UartFmcwRadarAdapter(SensorAdapter):
    """One physical radar unit on one serial port.

    Construction is cheap and does not touch the port — `start()` does, matching the contract
    in `SensorAdapter.start`'s docstring: constructing a full adapter set from configuration
    must not fail halfway through because one sensor happens to be unplugged.

    A dropped or never-present link is not treated as a caller error: `start()` and
    `get_detections()` never raise for it. The fault is visible in `health()`, and
    `get_detections()` keeps retrying the connection on `serial_config.reconnect_interval_s`,
    so a sensor plugged back in mid-run recovers on its own.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        sensor_id: str,
        position_enu: EnuPoint,
        frame: LocalFrame,
        serial_config: SerialLinkConfig,
        mount_config: RadarMountConfig,
        transport: SerialTransport | None = None,
    ) -> None:
        super().__init__(clock, sensor_id, SensorType.RADAR)
        self._position = position_enu
        self._frame = frame
        self._serial_config = serial_config
        self._mount = mount_config
        self._transport = transport or SerialTransport(
            serial_config.port, serial_config.baud_rate, serial_config.read_timeout_s
        )
        self._ids = detection_id_generator(sensor_id)

        self._running = False
        self._ever_started = False
        self._buffer = b""
        self._last_link_attempt_monotonic: float | None = None
        self._last_link_error: str | None = None
        self._detections_emitted = 0
        self._last_detection_time: datetime | None = None
        self._frame_successes = 0
        self._frame_errors = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ever_started = True
        self._buffer = b""
        self._detections_emitted = 0
        self._last_detection_time = None
        self._frame_successes = 0
        self._frame_errors = 0

        # Clear the reconnect backoff before attempting. An explicit start() is a deliberate
        # act by a caller who may well have just fixed the cable, and honouring a backoff armed
        # by the previous run would leave a working sensor reporting OFFLINE until it expired —
        # indistinguishable, to an operator, from hardware that is still unplugged (FM-010).
        self._last_link_attempt_monotonic = None
        self._try_connect()

    def stop(self) -> None:
        self._running = False
        self._transport.close()

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

    def get_detections(self) -> list[Detection]:
        if not self._ever_started:
            raise SensorAdapterError(
                f"sensor {self.sensor_id} polled before start(): an adapter nobody started "
                "reports an empty sky, which is indistinguishable from a quiet one"
            )
        if not self._running:
            return []

        self._try_connect()
        if not self._transport.is_open:
            return []

        try:
            chunk = self._transport.read_available()
        except SerialTransportError as exc:
            self._last_link_error = str(exc)
            self._transport.close()
            # Drop the partial frame with the link that was carrying it. Whatever arrives after
            # a reconnect is a fresh stream, and splicing it onto the tail of the old one can
            # only manufacture a frame that was never transmitted.
            self._buffer = b""
            return []

        self._buffer += chunk
        scan = extract_data_frames(self._buffer)
        self._buffer = scan.remainder
        self._frame_errors += scan.resyncs

        timestamp = self._clock.now()
        detections: list[Detection] = []
        for payload in scan.payloads:
            try:
                targets = parse_targets(payload)
            except FrameError:
                self._frame_errors += 1
                continue
            self._frame_successes += 1
            for raw in targets:
                detection = self._build_detection(raw, timestamp)
                if detection is not None:
                    detections.append(detection)

        if detections:
            self._detections_emitted += len(detections)
            self._last_detection_time = timestamp
        return detections

    def _try_connect(self) -> None:
        if self._transport.is_open:
            return
        now = self._clock.monotonic()
        if (
            self._last_link_attempt_monotonic is not None
            and now - self._last_link_attempt_monotonic < self._serial_config.reconnect_interval_s
        ):
            return
        self._last_link_attempt_monotonic = now
        try:
            self._transport.open()
            self._last_link_error = None
        except SerialTransportError as exc:
            self._last_link_error = str(exc)

    def _assess(self) -> tuple[SensorState, str]:
        if not self._ever_started:
            return SensorState.NOT_STARTED, "constructed but not started"
        if not self._running:
            return SensorState.OFFLINE, "stopped"
        if not self._transport.is_open:
            return SensorState.OFFLINE, self._last_link_error or "serial link not open"

        total_events = self._frame_successes + self._frame_errors
        if total_events >= _MIN_EVENTS_FOR_ERROR_RATE:
            error_rate = self._frame_errors / total_events
            if error_rate > _DEGRADED_FRAME_ERROR_RATE:
                return (
                    SensorState.DEGRADED,
                    f"{self._frame_errors}/{total_events} frames since start lost framing or "
                    "failed to parse; link may be marginal",
                )
        return SensorState.OK, "nominal"

    def _build_detection(self, raw: RawTarget, timestamp: datetime) -> Detection | None:
        if not (self._mount.min_valid_range_m <= raw.distance_m <= self._mount.max_valid_range_m):
            return None
        if raw.signal_raw < self._mount.min_signal_raw:
            return None

        azimuth_rad = math.radians((self._mount.boresight_deg + raw.angle_deg) % 360.0)
        tilt_rad = math.radians(self._mount.tilt_deg)

        horizontal_m = raw.distance_m * math.cos(tilt_rad)
        up_offset_m = raw.distance_m * math.sin(tilt_rad)
        east_offset_m = horizontal_m * math.sin(azimuth_rad)
        north_offset_m = horizontal_m * math.cos(azimuth_rad)

        geodetic = self._frame.to_geodetic(
            EnuPoint(
                east_m=self._position.east_m + east_offset_m,
                north_m=self._position.north_m + north_offset_m,
                up_m=self._position.up_m + up_offset_m,
            )
        )

        return Detection(
            detection_id=self._ids.next(),
            timestamp=timestamp,
            sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
            position=GeoPosition(
                latitude=geodetic.latitude_deg,
                longitude=geodetic.longitude_deg,
                altitude_m=geodetic.altitude_m,
            ),
            measurement=self._measurement_at(raw.distance_m, azimuth_rad),
            quality=self._quality_for(raw.signal_raw),
            kinematics=self._kinematics_for(raw),
            classification=None,
        )

    def _measurement_at(self, distance_m: float, azimuth_rad: float) -> Measurement:
        """Anisotropic covariance from range/cross-range/vertical sigmas, rotated by azimuth.

        Same construction the simulated adapter uses for the same reason: a scalar alone
        cannot express that this sensor is far more certain along the line of sight than
        across it, and the filter should be told the difference rather than left to assume
        isotropic error.
        """
        range_sigma = self._mount.range_sigma_m
        cross_sigma = distance_m * math.sin(math.radians(self._mount.azimuth_uncertainty_deg))
        vertical_sigma = max(
            self._mount.vertical_sigma_floor_m,
            distance_m * math.sin(math.radians(self._mount.elevation_uncertainty_deg)),
        )

        sin_az = math.sin(azimuth_rad)
        cos_az = math.cos(azimuth_rad)
        range_var = range_sigma**2
        cross_var = cross_sigma**2
        east_var = range_var * sin_az**2 + cross_var * cos_az**2
        north_var = range_var * cos_az**2 + cross_var * sin_az**2
        east_north_cov = (range_var - cross_var) * sin_az * cos_az

        return Measurement(
            position_uncertainty_m=max(range_sigma, cross_sigma, vertical_sigma),
            position_covariance_enu=[
                [east_var, east_north_cov, 0.0],
                [east_north_cov, north_var, 0.0],
                [0.0, 0.0, vertical_sigma**2],
            ],
        )

    def _quality_for(self, signal_raw: int) -> Quality:
        floor = self._mount.confidence_signal_floor_raw
        span = max(self._mount.confidence_signal_ceiling_raw - floor, 1)
        fraction = min(max((signal_raw - floor) / span, 0.0), 1.0)
        confidence = self._mount.confidence_min + (
            self._mount.confidence_max - self._mount.confidence_min
        ) * fraction
        return Quality(confidence=confidence)

    def _kinematics_for(self, raw: RawTarget) -> Kinematics:
        speed_mps = raw.speed_kph / 3.6
        direction_is_zero = raw.direction_byte == 0x00
        approaching = direction_is_zero == self._mount.direction_zero_means_approaching
        radial_velocity_mps = -speed_mps if approaching else speed_mps
        return Kinematics(radial_velocity_mps=radial_velocity_mps, provenance=Provenance.MEASURED)
