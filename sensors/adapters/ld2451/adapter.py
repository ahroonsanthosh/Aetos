"""The adapter: a serial link at one end, canonical Detections at the other.

Everything this device is peculiar about has already been dealt with by the time a Detection
leaves here. The decoder handled the wire format, the transport handled the port, and this
module handles the three things left — when the observation happened, where it happened, and
how much any of that can be trusted.

## Reading happens on its own thread

Serial reads block, and the pipeline steps synchronously on the event loop. So a reader thread
owns the port and the decoder, and `get_detections()` drains what it has produced.

The queue between them is bounded, and when it fills the **oldest** frame is dropped and
counted (ADR 0008). Oldest rather than newest because a live picture wants the freshest
observation, and a stale detection would be gated out by the tracker anyway. What is forbidden
is dropping silently, not dropping.

## Observation time is reconstructed, not guessed

A frame's last byte arriving is not the observation happening. Between the two sit the device's
own processing and the time the frame spent on the wire, and the second of those is exactly
computable:

    observation  =  arrival  −  frame_bytes × 10 / baud  −  sensor_latency

Ten bits per byte because the line is 8N1: start bit, eight data, stop. The sensor latency is
declared rather than measured — it cannot be seen from this end — and how wrong it is allowed
to be is `timing.timestamp_uncertainty_ms` in the registry entry.

Timing error is systematic, not random, so it does not average out. At 20 m/s every 100 ms of
error is two metres of position error in a fixed direction, which is why this is reconstructed
rather than approximated by "now".

## Altitude is assumed, and says so

The device has no elevation channel. Every detection is placed in the sensor's own horizontal
plane, marked `altitude_source = assumed`, and given a vertical variance derived from the beam's
extent at that range — about 15 m at 100 m. The filter therefore learns almost nothing about
altitude from this source, which is the truth (ADR 0019).

## A link that is probably lying is worse than one that is quiet

Past a configured windowed reject rate the adapter reports OFFLINE and emits nothing, rather
than continuing at reduced confidence. A quiet sensor produces coasting tracks and a visible
gap, both of which the system handles honestly. A lying one produces confident phantom targets,
which it does not (ADR 0018).
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.capabilities import (
    SensorCapabilities,
    TransportDeclaration,
    TransportIntegrity,
)
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.adapters.ld2451.config import Ld2451DriverConfig
from sensors.adapters.ld2451.link import LinkQualityWindow
from sensors.adapters.ld2451.protocol import (
    FrameDecoder,
    TargetFrame,
    TargetReport,
    assumed_altitude_sigma_m,
)
from sensors.adapters.ld2451.transport import SerialTransport, Transport, TransportError
from sensors.adapters.registry import LiveSensorModel
from sensors.schemas.detection import (
    AltitudeSource,
    Detection,
    GeoPosition,
    Kinematics,
    Measurement,
    Provenance,
    Quality,
    SensorRef,
)

BITS_PER_BYTE_8N1 = 10
"""Start bit, eight data bits, one stop bit. What a byte actually costs on this line."""

QUANTISATION_SIGMA = 1.0 / math.sqrt(12.0)
"""One-sigma of a value rounded to the nearest whole unit.

Range arrives as whole metres and angle as whole degrees, so each carries this much error before
the device's own accuracy is considered. Added in quadrature rather than ignored: it is small
against the sensor's error here, and writing it down is what stops someone later mistaking
resolution for accuracy."""

TransportFactory = Callable[[int], Transport]
"""Builds a transport for a given baud rate. A factory rather than an instance because baud
probing needs to open the link more than once, and because it is the seam a test replaces."""


class Ld2451Adapter(SensorAdapter):
    """One of these devices, on one serial port, at one place on a site."""

    def __init__(
        self,
        *,
        clock: Clock,
        sensor_id: str,
        model: LiveSensorModel,
        config: Ld2451DriverConfig,
        position_enu: EnuPoint,
        frame: LocalFrame,
        boresight_deg: float = 0.0,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        super().__init__(clock, sensor_id, model.sensor_type)
        config.check_against(model.coverage, model.timing)

        self._model = model
        self._config = config
        self._position = position_enu
        self._frame = frame
        self._boresight_deg = boresight_deg
        self._ids = detection_id_generator(sensor_id)
        self._new_transport: TransportFactory = transport_factory or (
            lambda baud: SerialTransport(config.port, baud)
        )

        self._decoder = FrameDecoder(config.plausibility)
        self._transport: Transport | None = None
        self._baud_rate: int | None = None

        self._lock = threading.Lock()
        self._queue: deque[tuple[datetime, TargetFrame]] = deque()
        self._window = LinkQualityWindow(clock, config.link_window_s)
        self._reader: threading.Thread | None = None

        self._running = False
        self._ever_started = False
        self._fault: str | None = None
        self._bytes_seen = 0
        self._frames_dropped = 0
        self._frames_suppressed = 0
        self._last_bytes_at: float | None = None
        self._last_detection_time: datetime | None = None
        self._detections_emitted = 0

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return

        self._fault = None
        self._transport, self._baud_rate = self._acquire_link()

        # Everything about the previous run is discarded, for the reason `SensorHealth` gives
        # for resetting the detection count: the watchdog reasons about rates over a window, and
        # counts that survived a restart would describe a run that has ended. A link that was
        # failing before a reconnection must be judged on what it does now.
        with self._lock:
            self._queue.clear()
            self._window = LinkQualityWindow(self._clock, self._config.link_window_s)
            self._bytes_seen = 0
            self._frames_dropped = 0
            self._frames_suppressed = 0
            self._last_bytes_at = self._clock.monotonic()

        self._running = True
        self._ever_started = True
        self._detections_emitted = 0
        self._last_detection_time = None

        self._reader = threading.Thread(
            target=self._read_loop, name=f"ld2451-{self.sensor_id}", daemon=True
        )
        self._reader.start()

    def stop(self) -> None:
        self._running = False

        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            # Bounded by the read timeout, which is why that timeout is short: it is also how
            # long a stop takes. A daemon thread means a forgotten stop cannot hold the process
            # open, and the join means a remembered one does not race the port being closed.
            reader.join(timeout=self._config.read_timeout_s * 4 + 1.0)

        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

        with self._lock:
            self._queue.clear()

    def _acquire_link(self) -> tuple[Transport, int]:
        """Open the port and establish which baud rate the device is actually using.

        The wrong rate does not fail cleanly. It produces a stream of bytes that frame up to
        nothing, which looks exactly like a powered sensor detecting nothing — so the rate is
        established by decoding a frame at it, not by assuming.
        """
        attempted: list[str] = []
        for baud in self._candidate_bauds():
            transport = self._new_transport(baud)
            transport.open()

            decoded = self._probe(transport)
            if decoded:
                return transport, baud

            attempted.append(f"{baud} (no frames decoded)")
            transport.close()

        raise SensorAdapterError(
            f"sensor {self.sensor_id}: opened {self._config.port} but decoded no frames at any "
            f"candidate baud rate — tried {', '.join(attempted)}. Either nothing is "
            "transmitting (check 5 V power and that TX/RX are crossed), or the logic level is "
            "wrong (the adapter must be switched to 3.3 V), or this device is configured to a "
            "rate not in baud_rate_candidates."
        )

    def _candidate_bauds(self) -> list[int]:
        """The configured rate first, then the rest. Order matters only for speed, but a
        deployment that has already been calibrated should not pay the probe cost every start."""
        ordered = [self._config.baud_rate, *self._config.baud_rate_candidates]
        seen: set[int] = set()
        return [baud for baud in ordered if not (baud in seen or seen.add(baud))]

    def _probe(self, transport: Transport) -> bool:
        """Listen at one rate and report whether anything decoded.

        Bounded by both a deadline and a read count. The deadline is what matters against a real
        clock; the read count is what makes this terminate against a stepped one, which is how
        the probe is testable at all without waiting in real time.
        """
        decoder = FrameDecoder(self._config.plausibility)
        started = self._clock.monotonic()
        max_reads = max(1, math.ceil(self._config.probe_seconds / self._config.read_timeout_s))

        for _ in range(max_reads):
            if self._clock.monotonic() - started >= self._config.probe_seconds:
                break
            data = transport.read(self._config.read_chunk_bytes, self._config.read_timeout_s)
            if data and decoder.feed(data):
                self._decoder = decoder
                return True

        return False

    # ----------------------------------------------------------------------------------
    # The reader thread
    # ----------------------------------------------------------------------------------

    def _read_loop(self) -> None:
        transport = self._transport
        if transport is None:  # pragma: no cover — start() always sets one first
            return

        while self._running:
            try:
                data = transport.read(
                    self._config.read_chunk_bytes, self._config.read_timeout_s
                )
            except TransportError as exc:
                # Recorded and reported, never raised across the thread boundary. One failed
                # sensor must not stop the others, and a link that has died is a health fact
                # rather than a pipeline error (FM-011).
                with self._lock:
                    self._fault = f"link failed: {exc}"
                return

            arrived_at = self._clock.now()
            monotonic = self._clock.monotonic()

            if data:
                frames = self._decoder.feed(data)
                observation = arrived_at - self._flight_time()
                with self._lock:
                    self._bytes_seen += len(data)
                    self._last_bytes_at = monotonic
                    for frame in frames:
                        self._enqueue(observation, frame)

            with self._lock:
                self._window.observe(self._decoder.statistics, self._bytes_seen)

    def _flight_time(self) -> timedelta:
        """How long ago the observation behind a just-arrived frame actually happened.

        Transmission time is computed from the frame size and the rate; the device's internal
        latency is declared. A maximum-size frame is used for the transmission term rather than
        the actual one, because a read may carry several frames and attributing them all to the
        read's completion is already the approximation being made here — the error is bounded by
        the read timeout, which `check_against` holds below one update period for exactly this
        reason.
        """
        baud = self._baud_rate or self._config.baud_rate
        transmission_s = self._config.plausibility.max_frame_length * BITS_PER_BYTE_8N1 / baud
        return timedelta(seconds=transmission_s + self._config.sensor_latency_ms / 1000.0)

    def _enqueue(self, observation: datetime, frame: TargetFrame) -> None:
        """Hold a frame for the pipeline. Caller holds the lock."""
        if len(self._queue) >= self._config.queue_capacity:
            self._queue.popleft()
            self._frames_dropped += 1
        self._queue.append((observation, frame))

    # ----------------------------------------------------------------------------------
    # What the pipeline asks for
    # ----------------------------------------------------------------------------------

    def get_detections(self) -> list[Detection]:
        if not self._ever_started:
            raise SensorAdapterError(
                f"sensor {self.sensor_id} polled before start(): an adapter nobody started "
                "reports an empty sky, which is indistinguishable from a quiet one"
            )
        if not self._running:
            return []

        with self._lock:
            pending = list(self._queue)
            self._queue.clear()
            quality = self._window.quality()
            faulted = self._fault is not None

        if faulted:
            return []

        if quality.frame_reject_rate >= self._config.offline_reject_rate:
            # Drained and discarded rather than left to accumulate: the queue must not fill with
            # frames from a link already judged untrustworthy, and the count is what makes the
            # discarding visible in health rather than invisible in a deque.
            with self._lock:
                self._frames_suppressed += len(pending)
            return []

        detections = [
            detection
            for observation, frame in pending
            for detection in self._detections_from(observation, frame)
        ]

        if detections:
            self._detections_emitted += len(detections)
            self._last_detection_time = max(d.timestamp for d in detections)
        return detections

    def _detections_from(self, observation: datetime, frame: TargetFrame) -> list[Detection]:
        return [self._detection_from(observation, target) for target in frame.targets]

    def _detection_from(self, observation: datetime, target: TargetReport) -> Detection:
        bearing_deg = (
            self._boresight_deg + self._config.azimuth_sign * target.angle_deg
        ) % 360.0
        bearing_rad = math.radians(bearing_deg)
        range_m = float(target.distance_m)

        sigma_range, sigma_cross, sigma_up = self._sigmas_at(range_m)

        # No elevation channel, so the target is placed in the sensor's own horizontal plane.
        # The vertical variance below is what says how little that is worth (ADR 0019).
        east = self._position.east_m + range_m * math.sin(bearing_rad)
        north = self._position.north_m + range_m * math.cos(bearing_rad)
        geodetic = self._frame.to_geodetic(
            EnuPoint(east_m=east, north_m=north, up_m=self._position.up_m)
        )

        return Detection(
            detection_id=self._ids.next(),
            timestamp=observation,
            sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
            position=GeoPosition(
                latitude=geodetic.latitude_deg,
                longitude=geodetic.longitude_deg,
                altitude_m=geodetic.altitude_m,
                altitude_source=AltitudeSource.ASSUMED,
            ),
            measurement=Measurement(
                # The largest of the three axes, never their root-mean-square: a consumer
                # reading only the scalar must not end up believing this sensor is better than
                # it is in its worst direction, which here is always the assumed vertical.
                position_uncertainty_m=max(sigma_range, sigma_cross, sigma_up),
                position_covariance_enu=self._covariance(
                    bearing_rad, sigma_range, sigma_cross, sigma_up
                ),
            ),
            quality=Quality(confidence=self._confidence(target)),
            kinematics=Kinematics(
                # Range rate is genuinely measured by this device — it is FMCW Doppler, which is
                # why the canonical schema carries range rate separately from speed and heading
                # (ADR 0002). Negative is toward the sensor, per the schema's convention.
                radial_velocity_mps=(
                    -target.speed_kmh / 3.6 if target.approaching else target.speed_kmh / 3.6
                ),
                provenance=Provenance.MEASURED,
            ),
            # No classification block at all. This device reports no identity evidence, and an
            # empty one would be a claim it has nothing to support.
        )

    def _sigmas_at(self, range_m: float) -> tuple[float, float, float]:
        """Along-range, cross-range and vertical one-sigma at a given range.

        Quantisation is added in quadrature to the declared accuracy rather than being taken as
        the accuracy. Range arrives as whole metres, which is 0.29 m of quantisation against a
        metre or so of real error — small, and worth writing down so that nobody later reads the
        resolution as the accuracy.
        """
        sigma_range = math.hypot(self._config.range_sigma_m, QUANTISATION_SIGMA)
        sigma_azimuth_deg = math.hypot(self._config.azimuth_sigma_deg, QUANTISATION_SIGMA)
        sigma_cross = range_m * math.radians(sigma_azimuth_deg)
        sigma_up = assumed_altitude_sigma_m(range_m, self._config.vertical_aperture_deg)
        return sigma_range, sigma_cross, sigma_up

    @staticmethod
    def _covariance(
        bearing_rad: float, sigma_range: float, sigma_cross: float, sigma_up: float
    ) -> list[list[float]]:
        """The error ellipse, resolved into ENU.

        The horizontal block is the sensor-frame diagonal rotated by the bearing, because the
        error genuinely is shaped that way: precise along the line of sight and imprecise across
        it. Collapsing it to something isotropic would discard exactly the information that
        keeps two crossing tracks apart, and would misreport this sensor as far worse in range
        than it is.
        """
        sin_b = math.sin(bearing_rad)
        cos_b = math.cos(bearing_rad)
        range_var = sigma_range**2
        cross_var = sigma_cross**2

        east_var = range_var * sin_b**2 + cross_var * cos_b**2
        north_var = range_var * cos_b**2 + cross_var * sin_b**2
        east_north_cov = (range_var - cross_var) * sin_b * cos_b

        return [
            [east_var, east_north_cov, 0.0],
            [east_north_cov, north_var, 0.0],
            [0.0, 0.0, sigma_up**2],
        ]

    def _confidence(self, target: TargetReport) -> float:
        """The device's own signal-to-noise figure, rescaled to the canonical range.

        A monotone rescaling of what the device reported, and nothing more. It is **not** a
        calibrated probability that the return is real, and calling it one would be inventing a
        number: the device documents the field as 0–100 and does not say what it means
        statistically. Rescaling preserves the ordering the device intended without adding a
        claim it never made.
        """
        span = self._config.plausibility.max_snr - self._config.plausibility.min_snr
        if span <= 0:  # pragma: no cover — the envelope validator forbids it
            return 0.0
        fraction = (target.snr - self._config.plausibility.min_snr) / span
        return float(min(max(fraction, 0.0), 1.0))

    # ----------------------------------------------------------------------------------
    # What it says about itself
    # ----------------------------------------------------------------------------------

    def health(self) -> SensorHealth:
        state, reason = self._assess()
        return SensorHealth(
            sensor_id=self.sensor_id,
            sensor_type=self.sensor_type,
            state=state,
            last_detection_time=self._last_detection_time,
            detections_emitted=self._detections_emitted,
            reason=_fit(reason),
        )

    def _assess(self) -> tuple[SensorState, str]:
        if not self._ever_started:
            return SensorState.NOT_STARTED, "constructed but not started"
        if not self._running:
            return SensorState.OFFLINE, "stopped"

        with self._lock:
            fault = self._fault
            quality = self._window.quality()
            silent_for = (
                self._clock.monotonic() - self._last_bytes_at
                if self._last_bytes_at is not None
                else 0.0
            )
            dropped = self._frames_dropped
            suppressed = self._frames_suppressed

        if fault is not None:
            return SensorState.OFFLINE, fault

        if silent_for >= self._config.silence_timeout_s:
            # A link judgement, not a coverage one. This device transmits frames continuously
            # whether or not anything is in front of it, so no bytes at all means the cable, the
            # power or the port — which the adapter can know. Whether an empty *sky* is a fault
            # is a site policy and belongs to the watchdog, not here (FM-010).
            return SensorState.OFFLINE, (
                f"no bytes received for {silent_for:.1f}s on {self._link_description()}. This "
                "device transmits whenever powered, so this is the link and not an empty sky: "
                "check power, cable and port"
            )

        reject_rate = quality.frame_reject_rate
        residual = self._decoder.residual_corruption_probability
        undetected = quality.estimated_undetected_rate(residual)

        if reject_rate >= self._config.offline_reject_rate:
            return SensorState.OFFLINE, (
                f"{reject_rate:.0%} of frames rejected in {quality.window_s:.0f}s, over the "
                f"{self._config.offline_reject_rate:.0%} limit; about {undetected:.0%} of "
                "accepted frames are likely corrupt and undetectable. Emitting nothing — a "
                "source probably lying is worse than one that is quiet (ADR 0018)"
            )

        if reject_rate >= self._config.degraded_reject_rate:
            return SensorState.DEGRADED, (
                f"{reject_rate:.1%} of frames rejected in {quality.window_s:.0f}s, byte discard "
                f"{quality.byte_discard_rate:.1%}; about {undetected:.1%} of accepted frames "
                "likely corrupt and undetectable. Check the cable, the ground, and distance "
                "from switching supplies"
            )

        if dropped:
            return SensorState.DEGRADED, (
                f"{dropped} frame(s) dropped: the pipeline is not draining this sensor as fast "
                "as it produces. Detections are being lost, oldest first"
            )

        if suppressed:
            return SensorState.DEGRADED, (
                f"link recovered, but {suppressed} frame(s) were discarded while it was judged "
                "untrustworthy"
            )

        if quality.silent:
            return SensorState.OK, (
                f"connected on {self._link_description()}; no traffic yet in the last "
                f"{quality.window_s:.0f}s"
            )

        return SensorState.OK, (
            f"{quality.frames_decoded} frame(s) decoded over the last {quality.window_s:.0f} s "
            f"on {self._link_description()}, {reject_rate:.1%} rejected"
        )

    def _link_description(self) -> str:
        description = (
            self._transport.description if self._transport is not None else self._config.port
        )
        # Bounded because it can be an arbitrarily long device path, and it is concatenated into
        # a health reason that has a hard length limit. See `_fit`.
        return description if len(description) <= 48 else f"...{description[-45:]}"

    def capabilities(self) -> SensorCapabilities:
        """What this source is, taken from the registry entry plus one computed number.

        Coverage, timing, accuracy and dimensions are the deployment's declaration and are
        repeated rather than re-derived — they are the claim a customer was shown, and the
        conformance suite exists to hold this adapter to them.

        The residual corruption probability is the exception, and is computed by the decoder
        from the frame layout and the configured envelope. It is the one integrity figure on a
        checksumless link, and asserting it in a document rather than calculating it would make
        it worth nothing (ADR 0018).
        """
        return SensorCapabilities(
            sensor_id=self.sensor_id,
            sensor_type=self._model.sensor_type,
            description=self._model.description,
            coverage=self._model.coverage,
            timing=self._model.timing,
            accuracy=self._model.accuracy,
            dimensions=self._model.dimensions,
            transport=TransportDeclaration(
                kind="serial",
                integrity=TransportIntegrity.STRUCTURAL,
                residual_corruption_probability=(
                    self._decoder.residual_corruption_probability
                ),
            ),
            classifies=self._model.classifies,
            coverage_validated=self._model.coverage_validated,
        )

    # ----------------------------------------------------------------------------------
    # Diagnostics, for the bench tool rather than for the pipeline
    # ----------------------------------------------------------------------------------

    @property
    def baud_rate(self) -> int | None:
        """The rate probing settled on, or None before `start()`."""
        return self._baud_rate

    @property
    def frames_dropped(self) -> int:
        with self._lock:
            return self._frames_dropped

    def link_quality(self):
        """The current window. Used by the bench tool and by health; not part of the contract."""
        with self._lock:
            return self._window.quality()

    @property
    def decoder(self) -> FrameDecoder:
        """Cumulative wire statistics, for the bench tool. Not for the pipeline: what the
        pipeline needs to know about the link is already in `health()`, in words."""
        return self._decoder


HEALTH_REASON_LIMIT = 256
"""What `SensorHealth.reason` accepts. Mirrored here because exceeding it raises.

The bug this exists to prevent already happened once: the degraded and offline reasons carry
several formatted numbers, and a link that began failing produced a reason two characters over
the limit — so the adapter raised a validation error at precisely the moment it was trying to
report that something was wrong. A health record that crashes when the news is bad is worse than
one that says nothing.

The reasons are written to fit. This is the second line of defence, so that a future edit
lengthening one degrades the message rather than the adapter.
"""


def _fit(reason: str) -> str:
    """Keep a health reason inside the schema's limit, truncating rather than raising."""
    if len(reason) <= HEALTH_REASON_LIMIT:
        return reason
    return reason[: HEALTH_REASON_LIMIT - 1] + "…"


def build_ld2451_adapter(request) -> Ld2451Adapter:
    """Construct one of these from a registry entry and a placement.

    Registered as the `ld2451_uart` driver by `sensors/adapters/drivers.py`. The signature takes
    the factory's `LiveBuildRequest`, imported lazily inside the annotation-free body so that
    this module does not depend on the factory that dispatches to it.

    The driver's own configuration is validated **here** rather than by the registry loader. The
    registry validates what every sensor has in common and cannot know what a serial radar
    needs; a driver that accepted a raw mapping and reached for keys later would turn a
    configuration error into a runtime one, three hours into a run.
    """
    from sensors.adapters.factory import AdapterFactoryError  # noqa: PLC0415

    model = request.model
    try:
        config = Ld2451DriverConfig.model_validate(model.driver_config)
    except Exception as exc:  # noqa: BLE001 — pydantic's error is the useful part, not its type
        raise AdapterFactoryError(
            f"registry entry {model.registry_key!r} has an invalid driver_config for driver "
            f"{model.driver!r}:\n{exc}"
        ) from exc

    try:
        return Ld2451Adapter(
            clock=request.clock,
            sensor_id=request.sensor_id,
            model=model,
            config=config,
            position_enu=request.position_enu,
            frame=request.frame,
            boresight_deg=request.boresight_deg,
        )
    except ValueError as exc:
        # `check_against` refusals: the driver configuration and the entry's own declaration
        # disagree. Re-raised as a factory error so the caller sees a configuration problem
        # rather than a construction one, with the entry named.
        raise AdapterFactoryError(
            f"registry entry {model.registry_key!r}: {exc}"
        ) from exc
