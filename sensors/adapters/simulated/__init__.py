"""The simulated sensor adapter and the sensor model it degrades ground truth through."""

from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.degradation import NOMINAL, SensorDegradation
from sensors.adapters.simulated.registry import (
    SensorRegistryError,
    load_sensor_model,
    load_sensor_registry,
)
from sensors.adapters.simulated.rng import sensor_generator
from sensors.adapters.simulated.sensor_model import (
    SENSOR_MODEL_SCHEMA_VERSION,
    ClassificationConfusion,
    ConfidenceModel,
    Coverage,
    RadialVelocityModel,
    SensorModel,
    Uncertainty,
)
from sensors.adapters.simulated.truth import TruthObject, TruthSource

__all__ = [
    "NOMINAL",
    "SENSOR_MODEL_SCHEMA_VERSION",
    "ClassificationConfusion",
    "ConfidenceModel",
    "Coverage",
    "RadialVelocityModel",
    "SensorDegradation",
    "SensorModel",
    "SensorRegistryError",
    "SimulatedSensorAdapter",
    "TruthObject",
    "TruthSource",
    "Uncertainty",
    "load_sensor_model",
    "load_sensor_registry",
    "sensor_generator",
]
