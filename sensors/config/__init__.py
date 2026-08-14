"""Deployment configuration for physical sensors."""

from sensors.config.loader import (
    SENSOR_CONFIG_SCHEMA_VERSION,
    CameraConfig,
    ClassifierConfig,
    MotionConfig,
    PositionEnu,
    RadarConfig,
    Range,
    SensorConfig,
    SensorConfigError,
    SiteOrigin,
    StubRadarConfig,
    load_sensor_config,
)

__all__ = [
    "SENSOR_CONFIG_SCHEMA_VERSION",
    "CameraConfig",
    "ClassifierConfig",
    "MotionConfig",
    "PositionEnu",
    "RadarConfig",
    "Range",
    "SensorConfig",
    "SensorConfigError",
    "SiteOrigin",
    "StubRadarConfig",
    "load_sensor_config",
]
