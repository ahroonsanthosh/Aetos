"""A real, serial-attached FMCW radar as a source of canonical Detections.

The physical unit behind this adapter is testbed hardware and will be replaced. Everything
peculiar to it — framing, byte layout, command handshake, its missing elevation channel —
is inside this package and nowhere else. See `README.md` here and `docs/adr/0017`.
"""

from sensors.adapters.uart_fmcw_radar.adapter import UartFmcwRadarAdapter
from sensors.adapters.uart_fmcw_radar.config import (
    ConfidenceConfig,
    DetectionGateConfig,
    MountConfig,
    UartFmcwRadarConfig,
    UartLinkConfig,
    UncertaintyConfig,
)
from sensors.adapters.uart_fmcw_radar.link import (
    ByteLink,
    LinkError,
    SerialByteLink,
    SerialLibraryMissing,
    available_ports,
)
from sensors.adapters.uart_fmcw_radar.protocol import (
    FrameError,
    RadarReport,
    RadarTarget,
    ReportParser,
)

__all__ = [
    "ByteLink",
    "available_ports",
    "ConfidenceConfig",
    "DetectionGateConfig",
    "FrameError",
    "LinkError",
    "MountConfig",
    "RadarReport",
    "RadarTarget",
    "ReportParser",
    "SerialByteLink",
    "SerialLibraryMissing",
    "UartFmcwRadarAdapter",
    "UartFmcwRadarConfig",
    "UartLinkConfig",
    "UncertaintyConfig",
]
