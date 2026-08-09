"""Sensor adapters: the one interface every source of detections implements."""

from sensors.adapters.interface import (
    SensorAdapter,
    SensorAdapterError,
    SensorHealth,
    SensorState,
)

__all__ = ["SensorAdapter", "SensorAdapterError", "SensorHealth", "SensorState"]
