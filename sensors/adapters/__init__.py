"""Sensor adapters: the one interface every source of detections implements.

Two things live at this level and are shared by every adapter: the `SensorAdapter` contract
itself, and the `SensorCapabilities` declaration through which a source states what it is. The
conformance suite under `conformance/` measures one against the other.

Concrete adapters are sub-packages. Everything a particular device is peculiar about stops
inside its own, which is what makes the hardware replaceable.
"""

from sensors.adapters.capabilities import (
    SENSOR_CAPABILITIES_SCHEMA_VERSION,
    AccuracyDeclaration,
    Coverage,
    DimensionsDeclaration,
    FloorAssessment,
    FloorThresholds,
    SensorCapabilities,
    SensorFloor,
    TimingDeclaration,
    TransportDeclaration,
    TransportIntegrity,
    assess_floor,
)
from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)

__all__ = [
    "SENSOR_CAPABILITIES_SCHEMA_VERSION",
    "AccuracyDeclaration",
    "Coverage",
    "DimensionsDeclaration",
    "FloorAssessment",
    "FloorThresholds",
    "SensorAdapter",
    "SensorAdapterError",
    "SensorCapabilities",
    "SensorFloor",
    "SensorHealth",
    "SensorState",
    "TimingDeclaration",
    "TransportDeclaration",
    "TransportIntegrity",
    "assess_floor",
]
