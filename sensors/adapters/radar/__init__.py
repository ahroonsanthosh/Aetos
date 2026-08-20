"""Real radar hardware, read over a serial link.

The first adapter in Aetos that talks to a device rather than to a simulator. See `adapter.py`
for what the device measures and, more importantly, what it does not.
"""

from sensors.adapters.radar.adapter import UartRadarAdapter
from sensors.adapters.radar.config import RadarHardwareConfig, hardware_keys, load_radar_hardware
from sensors.adapters.radar.transport import (
    ByteTransport,
    ReplayTransport,
    SerialTransport,
    TransportError,
)

__all__ = [
    "ByteTransport",
    "RadarHardwareConfig",
    "ReplayTransport",
    "SerialTransport",
    "TransportError",
    "UartRadarAdapter",
    "hardware_keys",
    "load_radar_hardware",
]
