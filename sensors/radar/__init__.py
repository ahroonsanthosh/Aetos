"""Hardware radar drivers, behind a contract that outlives any one unit.

Import `RadarSensor`, `TargetDetection` and `SensorStatus` from here. Importing a concrete
driver is something only the code that *constructs* a sensor should do, and even that reads the
model designation out of configuration rather than branching on it.

See `sensors/radar/README.md` for the layering, `docs/adr/0017` for why there are two nested
interfaces, and `docs/radar_testbed.md` for wiring and the first-run checklist.
"""

from sensors.radar.config import RadarConfig, available_radar_configs, load_radar_config
from sensors.radar.radar_interface import (
    RadarConnectionState,
    RadarSensor,
    RadarSensorError,
    SensorStatus,
    TargetDetection,
)

__all__ = [
    "RadarConfig",
    "RadarConnectionState",
    "RadarSensor",
    "RadarSensorError",
    "SensorStatus",
    "TargetDetection",
    "available_radar_configs",
    "load_radar_config",
]
