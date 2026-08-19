"""A `SensorAdapter` for a UART-connected 24 GHz FMCW radar module — the testbed sensor.

See `README.md` in this directory before wiring this into anything that matters: it lists,
in one place, every value here that is a documented placeholder rather than a measured or
verified fact.
"""

from __future__ import annotations

from sensors.adapters.uart_fmcw_radar.adapter import UartFmcwRadarAdapter
from sensors.adapters.uart_fmcw_radar.config import RadarMountConfig, SerialLinkConfig

__all__ = ["RadarMountConfig", "SerialLinkConfig", "UartFmcwRadarAdapter"]
