"""Radar adapters.

`StubRadarAdapter` is the fabricated source in use today; `Ld2451RadarAdapter` is the empty
shell the physical unit will be implemented into. Both are exported so that the composition
point names one or the other in a single place — nothing else in the codebase imports either
class, because everything downstream reads `SensorAdapter`.

This package is the only place in the codebase that may name a specific radar unit or know
that a serial protocol exists.
"""

from sensors.adapters.radar.ld2451_radar_adapter import Ld2451RadarAdapter
from sensors.adapters.radar.stub_radar_adapter import StubRadarAdapter

__all__ = ["Ld2451RadarAdapter", "StubRadarAdapter"]
