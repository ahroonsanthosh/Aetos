"""The serial-attached FMCW radar as a `SensorAdapter`.

This is the first adapter in Aetos that talks to real hardware, and the whole point of it is
that nothing downstream can tell. It implements the same four methods as the simulator and
emits the same canonical `Detection`, so tracking, fusion, classification, risk and alerting
are untouched by its arrival — which is the claim `docs/adr/0002` makes and this adapter is
the first real test of.

## What this hardware cannot do, and how that is expressed honestly

The unit measures **range, azimuth and radial speed**. It does not measure elevation, and it
cannot be made to. The canonical schema requires an altitude, so the adapter must supply one,
and there are only two honest ways to do that: refuse to emit at all, or emit the beam-centre
altitude with a vertical uncertainty wide enough to say *we do not know this*. It does the
second — `position_covariance_enu` carries a vertical variance derived from the full elevation
ambiguity of the beam, which at every range is several times the horizontal variance, so the
filter discounts the altitude channel instead of believing a number nobody measured.
Collapsing that into the isotropic scalar would have made the tracker confidently wrong about
height, which is the failure `Measurement` exists to prevent (FM-051).

Two more things it deliberately does not do:

- **No classification hint.** The firmware is tuned for vehicles. It has no basis whatsoever
  for a statement about a small airframe, so it makes none, and the classification module
  weighs the evidence it does have instead of evidence this adapter invented (FM-052).
- **No `snr_db`.** The signal-quality byte is an uncalibrated integer. Reporting it as
  decibels would be a unit claim the hardware does not support.

## Timestamps

The report frame carries no timestamp, so the adapter stamps detections with the read time
from the injected clock. That is receipt time, later than observation time by the link and
buffering latency — bounded by one report period, tens of milliseconds. It is recorded here
because a latency that is written down can be corrected later, and one that is assumed to be
zero cannot (FM-050, FM-012).
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.adapters.uart_fmcw_radar.config import UartFmcwRadarConfig
from sensors.adapters.uart_fmcw_radar.link import ByteLink, LinkError, SerialByteLink
from sensors.adapters.uart_fmcw_radar.protocol import (
    Command,
    RadarTarget,
    ReportParser,
    build_command,
)
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

_UNIFORM_SIGMA_FACTOR = 1.0 / math.sqrt(3.0)
"""Standard deviation of a uniform distribution as a fraction of its half-width. Used for the
vertical channel, where the target is equally likely to be anywhere in the beam's elevation
extent — a uniform spread is the honest description of ignorance, not a Gaussian one."""


class UartFmcwRadarAdapter(SensorAdapter):
    """One serial-attached FMCW radar at one place, pointing one way.

    Construction is cheap and opens nothing; `start()` acquires the link, so building the
    adapter set from configuration cannot fail halfway through on a sensor whose USB adapter
    happens to be unplugged.
    """

    def __init__(
        self,
        clock: Clock,
        config: UartFmcwRadarConfig,
        link: ByteLink | None = None,
    ) -> None:
        super().__init__(clock, config.sensor_id, SensorType.RADAR)
        self._config = config
        self._link = link if link is not None else SerialByteLink(config.link)
        self._parser = ReportParser()
        self._ids = detection_id_generator(config.sensor_id)
        self._frame = LocalFrame(
            GeodeticPoint(
                latitude_deg=config.mount.latitude,
                longitude_deg=config.mount.longitude,
                altitude_m=config.mount.altitude_m,
            )
        )

        self._started = False
        self._detections_emitted = 0
        self._last_detection_time: datetime | None = None
        self._last_frame_monotonic: float | None = None
        self._link_error: str | None = None
        self._retry_after_monotonic: float | None = None
        self._raw_log: Path | None = config.raw_log_path

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        self._started = True
        self._parser.reset()
        self._detections_emitted = 0
        self._last_detection_time = None
        self._last_frame_monotonic = self._clock.monotonic()
        self._open_link()

    def stop(self) -> None:
        self._started = False
        self._link.close()
        self._parser.reset()

    def _open_link(self) -> None:
        """Open the link, recording rather than raising on failure.

        An unreachable sensor is a health state, not a crash: the rest of the site keeps
        running and the watchdog is told, in words, that this one is offline. Deliberately
        different from `SensorAdapterError`, which is for wiring mistakes — a misconfigured
        adapter is a bug, an unplugged one is Tuesday.
        """
        try:
            self._link.open()
            self._link_error = None
            self._retry_after_monotonic = None
        except LinkError as exc:
            self._link_error = str(exc)
            self._retry_after_monotonic = (
                self._clock.monotonic() + self._config.link.reconnect_backoff_s
            )

    def _reconnect_if_due(self) -> bool:
        """Retry a dropped link once its backoff has elapsed. True when the link is usable."""
        if self._link.is_open:
            return True
        due = self._retry_after_monotonic
        if due is not None and self._clock.monotonic() < due:
            return False
        self._parser.reset()
        self._open_link()
        return self._link.is_open

    # -- observation -------------------------------------------------------------------

    def get_detections(self) -> list[Detection]:
        if not self._started:
            raise SensorAdapterError(
                f"get_detections() before start() on sensor {self.sensor_id!r}"
            )
        if not self._reconnect_if_due():
            return []

        try:
            data = self._link.read(self._config.link.max_read_bytes)
        except LinkError as exc:
            self._link_error = str(exc)
            self._link.close()
            self._retry_after_monotonic = (
                self._clock.monotonic() + self._config.link.reconnect_backoff_s
            )
            return []

        if not data:
            return []

        timestamp = self._clock.now()
        self._log_raw(timestamp, data)

        detections: list[Detection] = []
        for report in self._parser.feed(data):
            self._last_frame_monotonic = self._clock.monotonic()
            for target in report.targets:
                detection = self._to_detection(target, timestamp)
                if detection is not None:
                    detections.append(detection)

        self._detections_emitted += len(detections)
        if detections:
            self._last_detection_time = timestamp
        return detections

    def _to_detection(self, target: RadarTarget, timestamp: datetime) -> Detection | None:
        """One sensor-relative target as a canonical Detection, or None if gated out."""
        gate = self._config.gate
        if not gate.min_range_m <= target.range_m <= gate.max_range_m:
            return None
        if target.quality < gate.min_quality:
            return None
        if abs(target.azimuth_deg) > self._config.mount.azimuth_half_width_deg:
            # Outside the physical beam. Either the frame was corrupt in a way that still
            # framed correctly, or the layout assumption is wrong — both are reasons not to
            # emit, and the raw log is where it can be investigated.
            return None

        bearing_deg = (self._config.mount.boresight_deg + target.azimuth_deg) % 360.0
        position, covariance = self._project(target.range_m, bearing_deg)

        variances = (covariance[0][0], covariance[1][1], covariance[2][2])
        scalar_sigma = math.sqrt(sum(variances) / 3.0)

        return Detection(
            detection_id=self._ids.next(),
            timestamp=timestamp,
            sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
            position=position,
            measurement=Measurement(
                position_uncertainty_m=scalar_sigma,
                position_covariance_enu=covariance,
            ),
            quality=Quality(confidence=self._confidence(target.quality), snr_db=None),
            kinematics=Kinematics(
                radial_velocity_mps=target.radial_velocity_mps,
                provenance=Provenance.MEASURED,
            ),
            classification=None,
        )

    def _project(
        self, range_m: float, bearing_deg: float
    ) -> tuple[GeoPosition, list[list[float]]]:
        """Range and bearing to a WGS-84 position, with the ENU covariance that goes with it.

        Elevation is the mount tilt, because that is where the beam centre points and the
        sensor cannot say anything more specific. The vertical variance that comes back is
        what carries that admission downstream.
        """
        mount = self._config.mount
        tilt_rad = math.radians(mount.tilt_deg)
        bearing_rad = math.radians(bearing_deg)

        ground_m = range_m * math.cos(tilt_rad)
        up_m = range_m * math.sin(tilt_rad)

        enu = EnuPoint(
            east_m=ground_m * math.sin(bearing_rad),
            north_m=ground_m * math.cos(bearing_rad),
            up_m=up_m,
        )
        geodetic = self._frame.to_geodetic(enu)
        position = GeoPosition(
            latitude=geodetic.latitude_deg,
            longitude=geodetic.longitude_deg,
            altitude_m=geodetic.altitude_m,
        )
        return position, self._covariance(range_m, bearing_rad, tilt_rad)

    def _covariance(
        self, range_m: float, bearing_rad: float, tilt_rad: float
    ) -> list[list[float]]:
        """3x3 ENU covariance for one report.

        Radar error is strongly anisotropic — precise along the line of sight, imprecise
        across it, and here effectively unbounded in the vertical. The along/across variances
        are rotated into ENU by the bearing; the vertical is the beam's elevation ambiguity
        treated as a uniform spread, plus what the range error contributes through the tilt.
        Cross terms between horizontal and vertical are left at zero: they are real but small
        next to a vertical variance dominated by not measuring elevation at all.
        """
        unc = self._config.uncertainty
        mount = self._config.mount

        along_sigma = unc.range_sigma_m
        cross_sigma = range_m * math.sin(math.radians(unc.azimuth_sigma_deg))

        horizontal_along = along_sigma * math.cos(tilt_rad)
        var_along = horizontal_along**2
        var_cross = cross_sigma**2

        sin_b, cos_b = math.sin(bearing_rad), math.cos(bearing_rad)
        var_e = var_along * sin_b**2 + var_cross * cos_b**2
        var_n = var_along * cos_b**2 + var_cross * sin_b**2
        cov_en = (var_along - var_cross) * sin_b * cos_b

        elevation_half_width_rad = math.radians(mount.elevation_ambiguity_deg) / 2.0
        vertical_sigma = math.hypot(
            range_m * math.sin(elevation_half_width_rad) * _UNIFORM_SIGMA_FACTOR,
            along_sigma * math.sin(tilt_rad),
        )

        return [
            [var_e, cov_en, 0.0],
            [cov_en, var_n, 0.0],
            [0.0, 0.0, vertical_sigma**2],
        ]

    def _confidence(self, quality: int) -> float:
        """Uncalibrated signal-quality byte to a bounded canonical confidence."""
        cfg = self._config.confidence
        span = cfg.quality_ceiling - cfg.quality_floor
        fraction = min(1.0, max(0.0, (quality - cfg.quality_floor) / span))
        return cfg.confidence_floor + fraction * (cfg.confidence_ceiling - cfg.confidence_floor)

    def _log_raw(self, timestamp: datetime, raw: bytes) -> None:
        """Append one timestamped read, hex-encoded, one line per poll.

        The bytes are logged **exactly as they arrived** and before anything interprets them,
        so the noise and the frames that failed to decode are in the log too. Those are the
        ones worth having: a session where the parser saw nothing is only explicable if the
        bytes it saw nothing in were kept.

        Failures here are swallowed on purpose: a full disk or a bad path must not stop a
        sensor reporting. The loss shows up as a gap in the log, which is visible, rather than
        as an adapter that stopped, which looks like an empty sky.
        """
        if self._raw_log is None:
            return
        try:
            self._raw_log.parent.mkdir(parents=True, exist_ok=True)
            with self._raw_log.open("a", encoding="ascii") as handle:
                handle.write(f"{timestamp.isoformat()} {raw.hex()}\n")
        except OSError:
            self._raw_log = None

    # -- configuration -----------------------------------------------------------------

    def configure(
        self,
        *,
        max_range_m: int | None = None,
        min_speed_kmh: int | None = None,
        direction_filter: int | None = None,
        consecutive_detections: int | None = None,
        sensitivity_threshold: int | None = None,
        extended_reports: int | None = None,
    ) -> None:
        """Write detection parameters to the unit, inside the required handshake.

        The unit only accepts commands between `enable configuration` and `end configuration`,
        and it reports nothing at all while in that mode. The handshake is therefore owned here
        and closed in a `finally`, so no caller can leave the sensor parked in a state where it
        looks alive and detects nothing.

        Raises `SensorAdapterError` when the link is down: silently not applying a sensitivity
        change, and then reading detections as though it had been applied, is how a test session
        produces data nobody can interpret afterwards.
        """
        if not self._link.is_open:
            raise SensorAdapterError(
                f"cannot configure sensor {self.sensor_id!r}: link is not open"
                + (f" ({self._link_error})" if self._link_error else "")
            )

        detection_params = (
            max_range_m is not None
            or direction_filter is not None
            or min_speed_kmh is not None
            or consecutive_detections is not None
        )
        sensitivity_params = sensitivity_threshold is not None or extended_reports is not None

        try:
            self._link.write(build_command(Command.ENABLE_CONFIG, b"\x01\x00"))
            if detection_params:
                self._link.write(
                    build_command(
                        Command.SET_DETECTION_PARAMS,
                        bytes(
                            (
                                _byte("max_range_m", max_range_m, 100),
                                _byte("direction_filter", direction_filter, 2),
                                _byte("min_speed_kmh", min_speed_kmh, 0),
                                _byte("consecutive_detections", consecutive_detections, 1),
                            )
                        ),
                    )
                )
            if sensitivity_params:
                self._link.write(
                    build_command(
                        Command.SET_SENSITIVITY,
                        bytes(
                            (
                                _byte("extended_reports", extended_reports, 0),
                                _byte("sensitivity_threshold", sensitivity_threshold, 4),
                            )
                        ),
                    )
                )
        except LinkError as exc:
            raise SensorAdapterError(
                f"configuring sensor {self.sensor_id!r} failed: {exc}"
            ) from exc
        finally:
            try:
                self._link.write(build_command(Command.END_CONFIG))
            except LinkError:
                # The link died mid-handshake. The reconnect path will reopen it, and a
                # power-cycled unit leaves configuration mode by itself.
                self._link.close()

    # -- health ------------------------------------------------------------------------

    def health(self) -> SensorHealth:
        """Facts, and a reason in words. No judgement about whether silence means broken.

        The one thing this does judge is its own link: a stream that is all discards is a link
        problem — the signature of a wrong baud rate — and reporting that as a quiet sky would
        hide the single most likely bring-up mistake behind the appearance of a working sensor.
        """
        state, reason = self._assess()
        return SensorHealth(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            state=state,
            last_detection_time=self._last_detection_time,
            detections_emitted=self._detections_emitted,
            reason=reason,
        )

    def _assess(self) -> tuple[SensorState, str]:
        stats = self._parser.stats

        if not self._started:
            return SensorState.NOT_STARTED, "constructed but not started"
        if not self._link.is_open:
            return SensorState.OFFLINE, self._link_error or "serial link is not open"

        if stats.consecutive_bytes_discarded >= self._config.framing_error_budget:
            return SensorState.DEGRADED, (
                f"{stats.consecutive_bytes_discarded} consecutive bytes discarded without a "
                f"valid frame at {self._config.link.baud} baud — check the configured baud "
                f"rate and the 3.3V logic-level switch"
            )

        silence_s = self._silence_s()
        if silence_s is not None and silence_s > self._config.stale_after_s:
            return SensorState.DEGRADED, (
                f"link open but no valid frame for {silence_s:.1f}s "
                f"({stats.frames_malformed} malformed frames since start)"
            )

        return SensorState.OK, (
            f"link open on {self._config.link.port} at {self._config.link.baud} baud; "
            f"{stats.frames_decoded} frames decoded, {stats.frames_malformed} malformed"
        )

    def _silence_s(self) -> float | None:
        if self._last_frame_monotonic is None:
            return None
        return self._clock.monotonic() - self._last_frame_monotonic


def _byte(name: str, value: int | None, default: int) -> int:
    """Validate one command byte. Out-of-range values are rejected rather than truncated: a
    silently wrapped sensitivity is a setting nobody can reconstruct from the session notes."""
    resolved = default if value is None else value
    if not 0 <= resolved <= 255:
        raise SensorAdapterError(f"{name}={resolved} is out of range for a single byte (0-255)")
    return resolved
