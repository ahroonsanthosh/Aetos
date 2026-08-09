"""Seeded randomness for the simulator, derived per sensor.

ADR 0008: every scenario declares a seed, and a scenario run is reproducible from that seed
alone. Two consequences shape this module.

**One generator per sensor, derived from (scenario seed, sensor ID).** A single shared
generator would make every sensor's noise depend on the draw order of every other, so adding a
fifth sensor to a scenario would silently change the four already there and every recorded
expectation about them. Deriving independent streams from the scenario seed keeps a sensor's
sequence a function of that sensor alone.

**The derivation must not use Python's `hash()`.** String hashing is salted per process, so a
generator seeded from `hash(sensor_id)` produces a different stream on every run — the exact
failure the seed exists to prevent, and one that looks like flakiness rather than a bug.
A stable digest is used instead.
"""

from __future__ import annotations

import hashlib

import numpy as np


def sensor_generator(scenario_seed: int, sensor_id: str) -> np.random.Generator:
    """The random stream belonging to one sensor in one scenario run.

    Reproducible across processes, machines and Python versions: `blake2b` is specified, where
    `hash()` is not.
    """
    if scenario_seed < 0:
        raise ValueError("scenario seed must be non-negative")

    digest = hashlib.blake2b(sensor_id.encode("utf-8"), digest_size=8).digest()
    sensor_entropy = int.from_bytes(digest, "big")
    return np.random.default_rng(np.random.SeedSequence([scenario_seed, sensor_entropy]))
