"""The adapter that puts a physical radar unit behind the canonical `SensorAdapter` contract.

This is where hardware stops being hardware. Above it, Aetos sees `Detection` objects and
cannot tell whether they came from a radar on a tripod or from the simulator. Below it, a
`RadarSensor` reports ranges and angles in its own frame and knows nothing about the world.

The adapter holds a `RadarSensor`, never a specific driver. It imports no driver module, never
reads `raw_source` to decide anything, and would work unchanged against a different unit — which
is the whole point, and is enforced by `tests/unit/test_module_boundaries.py`.

## What the adapter takes on

The obligations in `docs/detection_schema.md`, applied to this class of sensor:

- **Frame and datum.** The unit reports slant range and an angle off its own boresight. Turning
  that into WGS-84 with an ellipsoidal altitude needs the mount geometry and the site's local
  frame, both of which live here. Getting it wrong is a silent error of tens of metres, not a
  visible failure (FM-004).
- **Time.** `TargetDetection.timestamp` is monotonic, because that is all the driver needs for
  staleness. The wall-clock timestamp that leaves the system is attached here, from the injected
  clock, and is the time the *observation* was made rather than the time it was processed — the
  monotonic offset between decode and now is subtracted so a slow cycle does not backdate or
  post-date the return.
- **Honest uncertainty.** A full 3x3 covariance, dominated by a vertical term the size of the
  beam, because this unit does not measure elevation and must not pretend otherwise. See
  `sensors/radar/geometry.py`.
- **Provenance.** Radial velocity is a genuine Doppler measurement and is marked `measured`. No
  classification is emitted at all: the unit's firmware distinguishes nothing, and a label from
  here would be an invention weighted as evidence downstream (ADR 0002).

## What it deliberately does not do

No confidence modelling. `Quality.confidence` is a flat configured number, and both the config
field and this comment say so, because no measured detection statistics exist for a small
airframe against this unit. Deriving a confidence from its uncalibrated signal-strength index
would produce something that looks like a measurement and is not, and the risk engine is
entitled to treat confidence as meaningful. Until the test matrix in `docs/radar_testbed.md`
has been run, a flat honest number is the strongest claim available.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, LocalFrame
from backend.common.identifiers import detection_id_generator
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)
from sensors.radar.config import RadarConfig
from sensors.radar.geometry import (
    BeamUncertainty,
    MountGeometry,
    solve_beam,
    unknown_elevation_sigma_deg,
)
from sensors.radar.radar_interface import (
    RadarConnectionState,
    RadarSensor,
    RadarSensorError,
    SensorStatus,
    TargetDetection,
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


class HardwareRadarAdapter(SensorAdapter):
    """One physical radar unit at one place on one site, as a canonical detection source.

    Construction is cheap and opens no port; `start()` connects. That is the same lifecycle the
    simulated adapter has, and it is what lets the full sensor set be built from configuration
    without the build failing halfway through on the one unit that is unplugged.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        sensor_id: str,
        radar: RadarSensor,
        config: RadarConfig,
        position_enu: EnuPoint,
        frame: LocalFrame,
    ) -> None:
        super().__init__(clock, sensor_id, SensorType.RADAR)
        self._radar = radar
        self._config = config
        self._position = position_enu
        self._frame = frame
        self._ids = detection_id_generator(sensor_id)

        self._mount = MountGeometry(
            boresight_deg=config.mount.boresight_deg,
            tilt_deg=config.mount.tilt_deg,
            height_m=config.mount.height_m,
        )
        self._uncertainty = BeamUncertainty(
            range_sigma_m=config.uncertainty.range_sigma_m,
            azimuth_sigma_deg=config.uncertainty.azimuth_sigma_deg,
            elevation_sigma_deg=unknown_elevation_sigma_deg(config.mount.elevation_extent_deg),
        )

        self._running = False
        self._ever_started = False
        self._last_detection_time: datetime | None = None
        self._detections_emitted = 0

    def start(self) -> None:
        if self._running:
            return
        self._radar.connect()
        self._running = True
        self._ever_started = True
        self._detections_emitted = 0
        self._last_detection_time = None

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._radar.disconnect()

    def get_detections(self) -> list[Detection]:
        if not self._ever_started:
            raise SensorAdapterError(
                f"sensor {self.sensor_id} polled before start(): an adapter nobody started "
                "reports an empty sky, which is indistinguishable from a quiet one"
            )
        if not self._running:
            return []

        try:
            targets = self._radar.get_targets()
        except RadarSensorError as exc:
            # Translated rather than propagated: the pipeline is written against the adapter
            # contract, and a driver-layer exception crossing it would make the boundary this
            # class exists to hold porous in the one direction nobody thinks to check.
            raise SensorAdapterError(f"sensor {self.sensor_id}: {exc}") from exc

        observed_at = self._clock.now()
        monotonic_now = self._clock.monotonic()

        detections = [
            self._to_detection(target, observed_at, monotonic_now) for target in targets
        ]
        if detections:
            self._detections_emitted += len(detections)
            self._last_detection_time = max(detection.timestamp for detection in detections)
        return detections

    def _to_detection(
        self, target: TargetDetection, observed_at: datetime, monotonic_now: float
    ) -> Detection:
        """Build one canonical Detection from one target report."""
        solution = solve_beam(
            range_m=target.range_m,
            azimuth_deg=target.azimuth_deg,
            mount=self._mount,
            uncertainty=self._uncertainty,
            elevation_deg=target.elevation_deg,
        )

        position = self._frame.to_geodetic(
            EnuPoint(
                east_m=self._position.east_m + solution.offset.east_m,
                north_m=self._position.north_m + solution.offset.north_m,
                up_m=self._position.up_m + solution.offset.up_m,
            )
        )

        # The largest of the three one-sigma axes, as the scalar fallback. The mean would
        # understate the vertical span this sensor cannot resolve, and understating uncertainty
        # makes the filter overconfident — worse than reporting nothing at all.
        variances = [solution.covariance_enu[i][i] for i in range(3)]
        scalar_sigma = math.sqrt(max(variances))

        return Detection(
            detection_id=self._ids.next(),
            timestamp=self._observation_time(target, observed_at, monotonic_now),
            sensor=SensorRef(sensor_id=self.sensor_id, sensor_type=self.sensor_type),
            position=GeoPosition(
                latitude=position.latitude_deg,
                longitude=position.longitude_deg,
                altitude_m=position.altitude_m,
            ),
            measurement=Measurement(
                position_uncertainty_m=scalar_sigma,
                position_covariance_enu=solution.covariance_enu,
            ),
            quality=Quality(confidence=self._config.uncertainty.reported_confidence),
            kinematics=Kinematics(
                radial_velocity_mps=target.velocity_mps,
                provenance=Provenance.MEASURED,
            ),
            # No classification block. This unit distinguishes nothing, and an absent hint is
            # what the classification module is built to work without (ADR 0002).
            classification=None,
        )

    def _observation_time(
        self, target: TargetDetection, observed_at: datetime, monotonic_now: float
    ) -> datetime:
        """When the observation was made, not when it was drained from the driver.

        The driver stamps each target with the monotonic time its frame decoded. A poll can
        collect several frames spanning a cycle, so the elapsed monotonic interval since each
        decode is subtracted from wall time. Stamping them all with the drain time would compress
        a cycle's worth of returns onto one instant, and a tracker deriving velocity from
        positions would read that as an object that teleported and then stopped.
        """
        age_s = max(0.0, monotonic_now - target.timestamp)
        return observed_at - timedelta(seconds=age_s)

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

    def _assess(self) -> tuple[SensorState, str]:
        """Map the driver's view of its link onto the health vocabulary the watchdog reads.

        A reconnecting link is DEGRADED rather than OFFLINE: the sensor is not observing, but it
        is expected back without intervention, and an operator shown OFFLINE would go looking for
        a fault that is already resolving itself. A faulted link is OFFLINE, because it is not
        coming back on its own.
        """
        if not self._ever_started:
            return SensorState.NOT_STARTED, "constructed but not started"
        if not self._running:
            return SensorState.OFFLINE, "stopped"

        status = self._radar.get_status()
        match status.state:
            case RadarConnectionState.CONNECTED:
                return self._assess_connected(status)
            case RadarConnectionState.RECONNECTING:
                return SensorState.DEGRADED, status.reason
            case _:
                return SensorState.OFFLINE, status.reason

    def _assess_connected(self, status: SensorStatus) -> tuple[SensorState, str]:
        """Judge an open link that may still not be carrying anything we understand.

        A port that opens at the wrong baud rate is the most likely first failure of this
        integration, and it is invisible in every obvious place: the link is up, no exception is
        raised, and no detections appear. From the watchdog's side that is indistinguishable
        from a radar pointed at empty sky.

        It is distinguishable from *here*, though, and naming it is what DEGRADED is for: bytes
        are arriving and none of them are framing. That is a capability loss this adapter knows
        about and can describe, so it says so rather than reporting OK and leaving an operator to
        work it out from a flat detection count.
        """
        if status.frames_decoded == 0 and (status.bytes_discarded or status.malformed_frames):
            return SensorState.DEGRADED, (
                f"link open but no frame has decoded: {status.bytes_discarded} byte(s) "
                f"discarded, {status.malformed_frames} malformed frame(s). Confirm the baud "
                f"rate and the protocol frame constants before trusting this unit"
            )
        return SensorState.OK, status.reason
