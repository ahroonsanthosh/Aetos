"""The simulated sensor adapter and the sensor model it degrades ground truth through.

The registry loader is deliberately not re-exported here. It moved to
`sensors/adapters/registry.py` when live entries arrived: a loader that resolves both simulated
and live documents is not owned by the simulated package, and importing it from here would have
made every live adapter reach through the simulator to find its own configuration.
"""

from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.degradation import NOMINAL, SensorDegradation
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
    "SimulatedSensorAdapter",
    "TruthObject",
    "TruthSource",
    "Uncertainty",
    "sensor_generator",
]
