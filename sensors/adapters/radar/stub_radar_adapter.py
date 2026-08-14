"""A stub radar: plausible returns from nothing at all, so the pipeline can run today.

This exists for exactly one reason — to let ingestion, tracking and the operator interface be
built and exercised before a physical radar unit is on the bench. It observes nothing. Every
return it emits is drawn from the ranges in `sensor_config.yaml` and corresponds to no object.

## What it is not

It is not the simulator. `sensors/adapters/simulated/` degrades scenario ground truth through
a modelled sensor and is the thing to use for any question about tracker or fusion behaviour;
its returns are wrong in the specific ways a real radar is wrong. This stub has no truth
source and no sensor model, so its output is uncorrelated noise: useful for proving detections
flow end to end, worthless for evaluating anything that reasons about them.

## Replacement

This file is the only one deleted when a physical radar arrives. Nothing imports the class
directly except the composition point that builds the adapter set; everything downstream reads
`SensorAdapter`, so the swap is a change of which constructor is called and nothing else.

Honesty about that swap: this adapter reports `SIMULATED_RADAR`, never `RADAR`. A stub
claiming to be a real sensor would let a bench recording be mistaken for a live one, and
`SensorType.is_simulated` is what stops an operator being shown fabricated contacts as fact.
"""

from __future__ import annotations

import math
import random

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.config.loader import RadarConfig
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


class StubRadarAdapter(SensorAdapter):
    """Emits zero or one fabricated return per poll, at the configured rate.

    Two gates sit in front of every emission: a minimum interval taken from the injected
    clock, and a probability draw. The interval gate is what makes the configured rate mean
    something — probability alone is per-poll, so a driver polling at 200 Hz would turn a 0.15
    emission probability into thirty contacts a second.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        config: RadarConfig,
        frame: LocalFrame,
    ) -> None:
        super().__init__(clock, config.sensor_id, SensorType.SIMULATED_RADAR)
        self._config = config
        self._stub = config.stub
        self._frame = frame
        self._position = EnuPoint(
            config.position_enu.east_m,
            config.position_enu.north_m,
            config.position_enu.up_m,
        )
        self._rng = random.Random(self._stub.seed)
        self._ids = detection_id_generator(config.sensor_id)

        self._running = False
        self._ever_started = False
        self._last_emission_monotonic: float | None = None
        self._detections_emitted = 0
        self._last_detection_time = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ever_started = True
        self._last_emission_monotonic = None
        self._detections_emitted = 0
        self._last_detection_time = None

    def stop(self) -> None:
        self._running = False

    def health(self) -> SensorHealth:
        if not self._ever_started:
            state, reason = SensorState.NOT_STARTED, "stub radar constructed but not started"
        elif not self._running:
            state, reason = SensorState.OFFLINE, "stub radar stopped"
        else:
            state, reason = (
                SensorState.OK,
                "stub radar running; returns are fabricated and correspond to no object",
            )
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
        if not self._interval_elapsed():
            return []
        if self._rng.random() >= self._stub.emission_probability:
            return []

        self._last_emission_monotonic = self._clock.monotonic()
        detection = self._fabricate()
        self._detections_emitted += 1
        self._last_detection_time = detection.timestamp
        return [detection]

    def _interval_elapsed(self) -> bool:
        """Whether enough time has passed since the last emission to allow another.

        `monotonic()` rather than `now()`: this is interval logic, and wall time can step
        backwards under clock correction (ADR 0003).
        """
        if self._last_emission_monotonic is None:
            return True
        elapsed = self._clock.monotonic() - self._last_emission_monotonic
        return elapsed >= self._stub.min_interval_s

    def _fabricate(self) -> Detection:
        """One return drawn from the configured ranges, converted to the canonical schema.

        Bearing and slant range are what a radar natively reports; the canonical schema speaks
        WGS-84, so the conversion happens here, which is the adapter's job (obligation 2 on
        `SensorAdapter`). Nothing downstream ever sees a bearing.
        """
        stub = self._stub
        bearing_deg = self._rng.uniform(stub.bearing_deg.min, stub.bearing_deg.max) % 360.0
        range_m = self._rng.uniform(stub.range_m.min, stub.range_m.max)
        velocity_mps = self._rng.uniform(stub.velocity_mps.min, stub.velocity_mps.max)
        confidence = self._rng.uniform(stub.confidence.min, stub.confidence.max)

        true_bearing_rad = math.radians((bearing_deg + self._config.boresight_deg) % 360.0)
        # Bearing is clockwise from north, so north is the cosine component and east the sine.
        east = self._position.east_m + range_m * math.sin(true_bearing_rad)
        north = self._position.north_m + range_m * math.cos(true_bearing_rad)

        geodetic = self._frame.to_geodetic(EnuPoint(east, north, self._position.up_m))

        return Detection(
            detection_id=self._ids.next(),
            timestamp=self._clock.now(),
            sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
            position=GeoPosition(
                latitude=geodetic.latitude_deg,
                longitude=geodetic.longitude_deg,
                altitude_m=geodetic.altitude_m,
            ),
            measurement=Measurement(position_uncertainty_m=stub.position_uncertainty_m),
            quality=Quality(confidence=confidence),
            # A radar measures range rate, not speed over ground, so the drawn velocity is
            # reported as radial velocity. Putting it in `speed_mps` would assert a quantity
            # no radar measures, and the tracker would be right to distrust it.
            kinematics=Kinematics(
                radial_velocity_mps=velocity_mps,
                provenance=Provenance.MEASURED,
            ),
            # Radar does not classify. Leaving this None is the honest report, and the
            # classification module is required to reach a defensible label without it.
            classification=None,
        )
