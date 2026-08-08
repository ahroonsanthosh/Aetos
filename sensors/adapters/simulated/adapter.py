"""The simulated sensor adapter: ground truth in, degraded observations out.

This adapter exists to be *wrong* in the specific ways a real sensor is wrong. It knows every
object's exact position, and it must never say so. Between the truth source and an emitted
Detection it applies, in order:

1. **Coverage.** Range limits, azimuth sector, elevation bounds. Outside them, nothing.
2. **Probability of detection.** Inside coverage is not the same as seen.
3. **Measurement noise**, anisotropic, in the sensor's own along-/across-line-of-sight frame.
4. **Classification confusion**, so the label emitted is a draw and not a copy of the truth.
5. **False alarms**, generated from nothing at all, shaped exactly like real returns.

Every one of those is the difference between a tracker that works and a tracker that only
appears to. Emitting clean truth is a bug, not a shortcut — ADR 0002.

## Why false alarms are built to be indistinguishable

A spurious return here carries the same uncertainty, the same confidence distribution and the
same optional blocks as a real one, and the emitted order is shuffled. It would be easier to
mark them, or to give them a distinctive low confidence, or to append them after the real
returns. Any of those would let a downstream module separate real from spurious perfectly,
which is privileged information no real sensor supplies — the same failure ADR 0002 is about,
arriving through a different door.

## Provenance

Everything optional this adapter emits is marked `simulated_truth`, including the noised
radial velocity. Range rate *is* a quantity a Doppler-capable sensor genuinely measures, so a
future real adapter would rightly mark it `measured`; but the number this adapter produces
originates in the simulator's own state however much noise is added to it, and the provenance
marker records where a value came from, not how plausible it looks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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
from sensors.adapters.simulated.degradation import NOMINAL, SensorDegradation
from sensors.adapters.simulated.rng import sensor_generator
from sensors.adapters.simulated.sensor_model import SensorModel
from sensors.adapters.simulated.truth import TruthObject, TruthSource
from sensors.schemas.detection import (
    ClassificationHint,
    ClassificationLabel,
    Detection,
    GeoPosition,
    Kinematics,
    Measurement,
    Provenance,
    Quality,
    SensorRef,
)

_UPDATE_TOLERANCE_S = 1e-9
"""Slack on the update-rate gate. A driver stepping a SimClock by exactly one update period
lands on the boundary, and floating-point representation of e.g. 1/3 s would otherwise make
every second update slip a cycle."""

FALSE_ALARM_RADIAL_VELOCITY_SIGMA_MPS = 8.0
"""Spread of the range rate attached to a spurious return by a Doppler-capable model. A false
alarm has no underlying motion, but it must still look like something moving, or its
kinematics block would identify it."""


@dataclass(frozen=True, slots=True)
class _Geometry:
    """Where an object sits relative to the sensor, in the sensor's own terms."""

    slant_range_m: float
    azimuth_rad: float
    delta_east_m: float
    delta_north_m: float
    delta_up_m: float


@dataclass(frozen=True, slots=True)
class _Observation:
    """A detection assembled but not yet identified.

    Identifiers are assigned after the emission order is shuffled, so that the sequence of
    detection IDs carries no information about which returns were real.
    """

    position: GeoPosition
    measurement: Measurement
    quality: Quality
    kinematics: Kinematics | None
    classification: ClassificationHint | None


class SimulatedSensorAdapter(SensorAdapter):
    """One simulated sensor at one place on one site.

    Construction is cheap and side-effect free; `start()` begins the update cadence. The
    adapter holds no scenario state of its own — it asks the truth source what is in the air
    whenever its own update period has elapsed, which is what lets sensors of different update
    rates coexist without a central scheduler.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        sensor_id: str,
        model: SensorModel,
        position_enu: EnuPoint,
        frame: LocalFrame,
        truth_source: TruthSource,
        scenario_seed: int,
        boresight_deg: float = 0.0,
    ) -> None:
        super().__init__(clock, sensor_id, model.sensor_type)
        self._model = model
        self._position = position_enu
        self._frame = frame
        self._truth = truth_source
        self._boresight_rad = math.radians(boresight_deg)
        self._rng = sensor_generator(scenario_seed, sensor_id)
        self._ids = detection_id_generator(sensor_id)

        self._running = False
        self._ever_started = False
        self._degradation = NOMINAL
        self._last_update_monotonic: float | None = None
        self._last_detection_time: datetime | None = None
        self._detections_emitted = 0

    @property
    def model(self) -> SensorModel:
        return self._model

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ever_started = True
        self._last_update_monotonic = None
        self._detections_emitted = 0
        self._last_detection_time = None

    def stop(self) -> None:
        self._running = False

    def apply_degradation(self, degradation: SensorDegradation) -> None:
        """Script a change in performance, per the scenario's sensor availability windows.

        Simulator-only, and deliberately absent from `SensorAdapter`: a real sensor degrades
        because something has happened to it, not because it was told to.
        """
        self._degradation = degradation

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

        elapsed = self._elapsed_since_last_update()
        if elapsed is None:
            return []
        self._last_update_monotonic = self._clock.monotonic()

        # An offline sensor has no receiver running, so it produces no false alarms either.
        # Returning before any draw also keeps the noise stream unconsumed while it is down.
        if self._degradation.offline:
            return []

        timestamp = self._clock.now()
        observations = [
            observation
            for obj in self._truth.objects_at(timestamp)
            if (observation := self._observe(obj)) is not None
        ]
        observations.extend(self._false_alarms(elapsed))

        order = self._rng.permutation(len(observations))
        detections = [
            Detection(
                detection_id=self._ids.next(),
                timestamp=timestamp,
                sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
                position=observations[index].position,
                measurement=observations[index].measurement,
                quality=observations[index].quality,
                kinematics=observations[index].kinematics,
                classification=observations[index].classification,
            )
            for index in order
        ]

        if detections:
            self._detections_emitted += len(detections)
            self._last_detection_time = timestamp
        return detections

    def _elapsed_since_last_update(self) -> float | None:
        """Seconds of dwell this update covers, or None if the update period has not elapsed.

        Uses `monotonic()` rather than `now()` because this is interval logic: wall time can
        step backwards under clock correction, and a sensor that stopped updating until wall
        time caught up would be a bug visible only during an NTP adjustment (ADR 0003).
        """
        now = self._clock.monotonic()
        if self._last_update_monotonic is None:
            # First update after start. Charge one nominal period so the false-alarm rate is
            # not distorted by however long the adapter happened to sit idle beforehand.
            return self._model.update_period_s

        elapsed = now - self._last_update_monotonic
        if elapsed < self._model.update_period_s - _UPDATE_TOLERANCE_S:
            return None
        return elapsed

    def _assess(self) -> tuple[SensorState, str]:
        if not self._ever_started:
            return SensorState.NOT_STARTED, "constructed but not started"
        if not self._running:
            return SensorState.OFFLINE, "stopped"
        if self._degradation.offline:
            return SensorState.OFFLINE, self._degradation.reason or "scripted offline"
        if not self._degradation.is_nominal:
            return SensorState.DEGRADED, self._degradation.reason or "operating below model"
        return SensorState.OK, "nominal"

    def _observe(self, obj: TruthObject) -> _Observation | None:
        """Model what this sensor would have reported of `obj`, or nothing at all."""
        geometry = self._geometry(obj.position)
        if geometry is None:
            return None

        probability = (
            self._model.probability_of_detection * self._degradation.detection_probability_scale
        )
        if self._rng.random() >= probability:
            return None

        return _Observation(
            position=self._noisy_position(geometry),
            measurement=self._measurement_at(geometry),
            quality=self._quality_at(geometry.slant_range_m, is_false_alarm=False),
            kinematics=self._kinematics_for(obj, geometry),
            classification=self._classify(obj.label),
        )

    def _geometry(self, position: EnuPoint) -> _Geometry | None:
        """Range, azimuth and elevation to a point, or None if it lies outside coverage."""
        delta_east = position.east_m - self._position.east_m
        delta_north = position.north_m - self._position.north_m
        delta_up = position.up_m - self._position.up_m

        horizontal = math.hypot(delta_east, delta_north)
        slant = math.hypot(horizontal, delta_up)

        coverage = self._model.coverage
        if slant < coverage.min_range_m or slant > coverage.max_range_m:
            return None

        elevation_deg = math.degrees(math.atan2(delta_up, horizontal))
        if not coverage.elevation_min_deg <= elevation_deg <= coverage.elevation_max_deg:
            return None

        azimuth = math.atan2(delta_east, delta_north) % (2.0 * math.pi)
        if not coverage.has_full_azimuth:
            half_extent = math.radians(coverage.azimuth_extent_deg) / 2.0
            offset = (azimuth - self._boresight_rad + math.pi) % (2.0 * math.pi) - math.pi
            if abs(offset) > half_extent:
                return None

        return _Geometry(slant, azimuth, delta_east, delta_north, delta_up)

    def _sigmas_at(self, slant_range_m: float) -> tuple[float, float, float]:
        """Along-range, cross-range and vertical one-sigma, with degradation applied."""
        uncertainty = self._model.uncertainty
        scale = self._degradation.noise_scale
        return (
            uncertainty.range_sigma_m * scale,
            uncertainty.cross_range_sigma_at_m(slant_range_m) * scale,
            uncertainty.vertical_sigma_m * scale,
        )

    def _noisy_position(self, geometry: _Geometry) -> GeoPosition:
        """Truth displaced by a draw from the model's error distribution.

        The draw is made in the sensor's frame — one component along the line of sight, one
        across it — and then resolved into ENU. Drawing in ENU directly would produce
        isotropic error and quietly discard the anisotropy that the covariance reports.
        """
        sigma_range, sigma_cross, sigma_up = self._sigmas_at(geometry.slant_range_m)
        along, across, vertical = self._rng.standard_normal(3)

        sin_az = math.sin(geometry.azimuth_rad)
        cos_az = math.cos(geometry.azimuth_rad)

        east = geometry.delta_east_m + sigma_range * along * sin_az + sigma_cross * across * cos_az
        north = (
            geometry.delta_north_m + sigma_range * along * cos_az - sigma_cross * across * sin_az
        )
        up = geometry.delta_up_m + sigma_up * vertical

        geodetic = self._frame.to_geodetic(
            EnuPoint(
                east_m=self._position.east_m + east,
                north_m=self._position.north_m + north,
                up_m=self._position.up_m + up,
            )
        )
        return GeoPosition(
            latitude=geodetic.latitude_deg,
            longitude=geodetic.longitude_deg,
            altitude_m=geodetic.altitude_m,
        )

    def _measurement_at(self, geometry: _Geometry) -> Measurement:
        """The uncertainty actually applied, resolved into ENU. Never an optimistic summary.

        The 2x2 horizontal block is the sensor-frame diagonal rotated by the azimuth, so it
        describes exactly the ellipse `_noisy_position` drew from. The scalar fallback is the
        largest of the three axes rather than their root-mean-square: a consumer that reads
        only the scalar must not end up believing the sensor is better than it is in its worst
        direction.
        """
        sigma_range, sigma_cross, sigma_up = self._sigmas_at(geometry.slant_range_m)
        sin_az = math.sin(geometry.azimuth_rad)
        cos_az = math.cos(geometry.azimuth_rad)

        range_var = sigma_range**2
        cross_var = sigma_cross**2
        east_var = range_var * sin_az**2 + cross_var * cos_az**2
        north_var = range_var * cos_az**2 + cross_var * sin_az**2
        east_north_cov = (range_var - cross_var) * sin_az * cos_az

        return Measurement(
            position_uncertainty_m=max(sigma_range, sigma_cross, sigma_up),
            position_covariance_enu=[
                [east_var, east_north_cov, 0.0],
                [east_north_cov, north_var, 0.0],
                [0.0, 0.0, sigma_up**2],
            ],
        )

    def _quality_at(self, slant_range_m: float, *, is_false_alarm: bool) -> Quality:
        """Source confidence, falling off with range and jittered.

        The false-alarm scale shifts the distribution down without separating it, so clutter
        is on average weaker than a real return but never identifiable from confidence alone.
        """
        confidence = self._model.confidence
        coverage = self._model.coverage
        span = coverage.max_range_m - coverage.min_range_m
        fraction = min(max((slant_range_m - coverage.min_range_m) / span, 0.0), 1.0)

        value = confidence.near_range + (confidence.far_range - confidence.near_range) * fraction
        if is_false_alarm:
            value *= confidence.false_alarm_scale
        if confidence.jitter_sigma > 0.0:
            value += self._rng.normal(0.0, confidence.jitter_sigma)

        return Quality(confidence=float(min(max(value, 0.0), 1.0)))

    def _kinematics_for(self, obj: TruthObject, geometry: _Geometry) -> Kinematics | None:
        """Range rate, if this model reports it. Never speed, never heading.

        Speed and heading are quantities a tracker derives from a sequence of positions, and
        no sensor this project models measures them. Emitting them — even noised, even marked
        — would hand the filter an answer real hardware never supplies (ADR 0002).
        """
        if not self._model.radial_velocity.reported:
            return None

        radial = (
            obj.velocity_east_mps * geometry.delta_east_m
            + obj.velocity_north_mps * geometry.delta_north_m
            + obj.velocity_up_mps * geometry.delta_up_m
        ) / geometry.slant_range_m
        sigma = self._model.radial_velocity.sigma_mps * self._degradation.noise_scale
        noise = self._rng.normal(0.0, sigma) if sigma > 0.0 else 0.0

        return Kinematics(
            radial_velocity_mps=float(radial + noise),
            provenance=Provenance.SIMULATED_TRUTH,
        )

    def _classify(self, true_label: ClassificationLabel) -> ClassificationHint | None:
        """A draw from the confusion model. Never a copy of `true_label`."""
        confusion = self._model.classification
        if confusion is None:
            return None

        if self._rng.random() < confusion.correct_probability:
            return ClassificationHint(
                label=true_label,
                confidence=confusion.confidence_when_correct,
                provenance=Provenance.SIMULATED_TRUTH,
            )

        # The truth is excluded from the alternatives, so a "wrong" draw is genuinely wrong.
        # If a profile lists nothing else, the honest report is that it does not know.
        alternatives = [label for label in confusion.confusable_with if label != true_label]
        label = self._pick(alternatives) if alternatives else ClassificationLabel.UNKNOWN
        return ClassificationHint(
            label=label,
            confidence=confusion.confidence_when_confused,
            provenance=Provenance.SIMULATED_TRUTH,
        )

    def _false_alarms(self, elapsed_s: float) -> list[_Observation]:
        """Returns generated from nothing, placed uniformly across the coverage volume.

        Poisson rather than a fixed count per update because clutter arrives in clumps, and a
        tracker that only ever sees one spurious return at a time is never asked the question
        scenario F exists to ask.
        """
        rate = self._model.false_alarm_rate_hz * self._degradation.false_alarm_rate_scale
        if rate <= 0.0:
            return []

        count = int(self._rng.poisson(rate * elapsed_s))
        return [self._false_alarm() for _ in range(count)]

    def _false_alarm(self) -> _Observation:
        coverage = self._model.coverage

        if coverage.has_full_azimuth:
            azimuth = self._rng.uniform(0.0, 2.0 * math.pi)
        else:
            half_extent = math.radians(coverage.azimuth_extent_deg) / 2.0
            azimuth = self._boresight_rad + self._rng.uniform(-half_extent, half_extent)

        # Square-rooted so clutter is uniform per unit *area*, not per unit range. Drawing
        # range uniformly would pile spurious returns up near the sensor, where they would
        # look like a real object loitering close in.
        near = coverage.min_range_m**2
        far = coverage.max_range_m**2
        slant = math.sqrt(self._rng.uniform(near, far))

        elevation = math.radians(
            self._rng.uniform(coverage.elevation_min_deg, coverage.elevation_max_deg)
        )
        horizontal = slant * math.cos(elevation)
        geometry = _Geometry(
            slant_range_m=slant,
            azimuth_rad=azimuth % (2.0 * math.pi),
            delta_east_m=horizontal * math.sin(azimuth),
            delta_north_m=horizontal * math.cos(azimuth),
            delta_up_m=slant * math.sin(elevation),
        )

        return _Observation(
            position=self._position_of(geometry),
            measurement=self._measurement_at(geometry),
            quality=self._quality_at(slant, is_false_alarm=True),
            kinematics=self._false_alarm_kinematics(),
            classification=self._false_alarm_classification(),
        )

    def _position_of(self, geometry: _Geometry) -> GeoPosition:
        """Geodetic position of an offset from the sensor, with no further noise applied.

        Used for false alarms, whose drawn position already *is* the measurement — there is no
        underlying truth for noise to displace.
        """
        geodetic = self._frame.to_geodetic(
            EnuPoint(
                east_m=self._position.east_m + geometry.delta_east_m,
                north_m=self._position.north_m + geometry.delta_north_m,
                up_m=self._position.up_m + geometry.delta_up_m,
            )
        )
        return GeoPosition(
            latitude=geodetic.latitude_deg,
            longitude=geodetic.longitude_deg,
            altitude_m=geodetic.altitude_m,
        )

    def _false_alarm_kinematics(self) -> Kinematics | None:
        if not self._model.radial_velocity.reported:
            return None
        return Kinematics(
            radial_velocity_mps=float(
                self._rng.normal(0.0, FALSE_ALARM_RADIAL_VELOCITY_SIGMA_MPS)
            ),
            provenance=Provenance.SIMULATED_TRUTH,
        )

    def _false_alarm_classification(self) -> ClassificationHint | None:
        confusion = self._model.classification
        if confusion is None:
            return None
        return ClassificationHint(
            label=self._pick(confusion.confusable_with),
            confidence=confusion.confidence_when_confused,
            provenance=Provenance.SIMULATED_TRUTH,
        )

    def _pick(self, labels: list[ClassificationLabel]) -> ClassificationLabel:
        """Uniform choice by index rather than `Generator.choice`, which would coerce the
        enum members through a NumPy string array and hand back something that is not a
        `ClassificationLabel`."""
        return labels[int(self._rng.integers(len(labels)))]
