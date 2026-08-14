"""The composition point for physical sensors: configuration in, adapters out.

**This is the one file to edit when a sensor is added, removed or replaced.** It is the only
place in the codebase that names a concrete adapter class alongside the configuration that
drives it. Everything downstream — `backend/main.py`, ingestion, tracking, classification,
risk — reads `SensorAdapter` and canonical `Detection`s and cannot tell what produced them.

## Replacing the stub radar with the physical unit

One line changes, on the line marked below:

    StubRadarAdapter(...)      ->      Ld2451RadarAdapter(...)

Both already satisfy `SensorAdapter`, both take the same `RadarConfig`, and both are
constructed with the same arguments. `stub_radar_adapter.py` is then deleted and no other file
in the repository is touched. That is the whole migration, and the boundary tests in
`tests/unit/test_module_boundaries.py` fail the build if anything else has quietly grown a
dependency that would make it larger.

## Frames, and why the site file wins

Adapters are handed the **backend's** local ENU frame, anchored at the site reference point
from `config/sites/`. The `site_origin` block in `sensor_config.yaml` is a fallback for the
standalone bench script, which runs without a site loaded; when the pipeline builds adapters
it passes its own frame and the site file is the single authority on where the site is. Two
sources of truth for a sensor's position is exactly the kind of error that produces detections
tens of metres from where the sensor really is, so there is only one.

`position_enu` in `sensor_config.yaml` is a sensor's offset *within* that frame, which is
frame-independent, so it is read from configuration in both cases.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.common.clock import Clock
from backend.common.geodesy import EnuPoint, LocalFrame
from sensors.adapters.camera.camera_adapter import CameraAdapter
from sensors.adapters.interface import SensorAdapter
from sensors.adapters.radar.stub_radar_adapter import StubRadarAdapter
from sensors.config.loader import PositionEnu, SensorConfig

CAMERA_REGISTRY_KEY = "live_camera"
"""Placeholder registry key for the camera's persisted record.

Live sensors have no entry in `config/sensor_registry/`, which describes *simulation*
characteristics — probability of detection, false alarm rate, confusion matrices — that a real
sensor does not need and must not be assigned. The key is carried because `SensorRecord`
requires one and the operator interface groups by it; it is not resolved against the registry.
"""

RADAR_REGISTRY_KEY = "live_radar"


@dataclass(frozen=True, slots=True)
class LiveSensor:
    """One physical sensor: the adapter that reads it, and where it is.

    Placement travels alongside the adapter rather than being looked up separately, so that
    adding a sensor is one entry in one list. If the caller had to enumerate sensors a second
    time to find their positions, the two lists would drift the first time one changed.
    """

    adapter: SensorAdapter
    position_enu: EnuPoint
    boresight_deg: float
    registry_key: str

    @property
    def sensor_id(self) -> str:
        return self.adapter.sensor_id


def build_live_sensors(
    *,
    clock: Clock,
    frame: LocalFrame,
    config: SensorConfig,
) -> list[LiveSensor]:
    """Every physical sensor this deployment is configured for.

    Construction is cheap and touches no hardware — `SensorAdapter.start()` is what opens a
    stream or a port. That matters here: building the full sensor set must not fail halfway
    through because one sensor happens to be unplugged, or a deployment could not start at all
    without every sensor present.
    """
    return [
        LiveSensor(
            adapter=CameraAdapter(
                clock=clock,
                config=config.camera,
                frame=frame,
            ),
            position_enu=_enu(config.camera.position_enu),
            boresight_deg=config.camera.boresight_deg,
            registry_key=CAMERA_REGISTRY_KEY,
        ),
        LiveSensor(
            # >>> THE RADAR SWAP HAPPENS HERE <<<
            # Replace StubRadarAdapter with Ld2451RadarAdapter when the unit arrives. Same
            # constructor arguments, same interface, no other change anywhere.
            adapter=StubRadarAdapter(
                clock=clock,
                config=config.radar,
                frame=frame,
            ),
            position_enu=_enu(config.radar.position_enu),
            boresight_deg=config.radar.boresight_deg,
            registry_key=RADAR_REGISTRY_KEY,
        ),
    ]


def _enu(position: PositionEnu) -> EnuPoint:
    return EnuPoint(position.east_m, position.north_m, position.up_m)
