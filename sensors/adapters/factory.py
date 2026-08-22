"""Turning configuration into adapters.

The one place that decides which implementation a `SensorPlacement` resolves to. Before this
existed, `backend/main.py` constructed `SimulatedSensorAdapter` directly and had no path that
could construct anything else — so "mode selection is adapter registry configuration rather than
a code path" (`docs/architecture.md`) was an intention rather than a mechanism. This is the
mechanism.

## Run modes, and what each one does with the other kind

A site document lists placements; the registry says what each one is. The mode decides which
kinds are built:

| Mode | Simulated placements | Live placements |
|---|---|---|
| SIMULATION | built | skipped, and reported |
| LIVE | skipped, and reported | built |
| HYBRID | built | built |

**Skipped, and reported** is the important half. A live-fitted site running a training scenario
must be able to run without its hardware, and a bench test of one real sensor must be able to
run without the simulator — but a placement that was configured and not built is a fact the
operator has to be able to see, or the sensor list silently disagrees with the site document and
the difference reads as "this site has fewer sensors" rather than "some were not started".

So nothing is dropped quietly. `AdapterSet.skipped` carries every one with the reason, and the
API reports it.

## Live drivers register themselves

`register_driver` is how a driver becomes buildable, and the factory has no import-time knowledge
of any concrete driver. Adding one is a new package under `sensors/adapters/` plus a
registration, and no edit here — which is the same property that makes a new sensor an addition
to one directory rather than a change to the pipeline.

With no drivers registered, a live placement fails to build with a message naming what is known.
That is the honest state of a system with no real sensor integration yet: not a silent skip, and
not a pretence that it worked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from backend.common.clock import Clock
from backend.common.config import RunMode
from backend.common.geodesy import EnuPoint, LocalFrame
from sensors.adapters.interface import SensorAdapter
from sensors.adapters.registry import LiveSensorModel, RegistryEntry
from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.sensor_model import SensorModel
from sensors.adapters.simulated.truth import TruthSource


class AdapterFactoryError(RuntimeError):
    """A placement could not be turned into an adapter.

    Raised rather than skipped. A placement the operator configured and the system could not
    build is a configuration error, and starting anyway with one fewer sensor is how a site ends
    up with a coverage gap nobody knows about.
    """


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one sensor sits, reduced to what an adapter needs.

    A plain value object rather than the site's `SensorPlacement`, so `sensors/` does not have to
    import `backend.zones` and thereby learn what a site or a zone is. The caller, which has
    both, does the conversion.
    """

    sensor_id: str
    registry_key: str
    position_enu: EnuPoint
    boresight_deg: float = 0.0


@dataclass(frozen=True, slots=True)
class SimulationContext:
    """What a simulated adapter needs beyond its placement: something to observe.

    Required only when simulated placements are being built. Absent, they are skipped with a
    reason rather than built against an empty world — a simulated sensor with no truth source
    would report a permanently empty sky, which is the one thing that must never be produced by
    accident (FM-010).
    """

    truth_source: TruthSource
    scenario_seed: int


@dataclass(frozen=True, slots=True)
class LiveBuildRequest:
    """Everything a live driver is handed to construct itself.

    Deliberately not the registry entry alone: an adapter converting range and bearing into
    WGS-84 needs to know where it is and which way it faces, and that is site configuration
    rather than sensor configuration.
    """

    clock: Clock
    sensor_id: str
    model: LiveSensorModel
    position_enu: EnuPoint
    frame: LocalFrame
    boresight_deg: float


DriverBuilder = Callable[[LiveBuildRequest], SensorAdapter]

_DRIVERS: dict[str, DriverBuilder] = {}


def register_driver(name: str, builder: DriverBuilder) -> None:
    """Make a live driver buildable under `name`, as referenced by a registry entry's `driver`.

    Re-registering the same name raises. A silently replaced driver would mean the adapter that
    runs is not the one the reviewer of the registry document believed would run, which is the
    same class of mistake the registry's filename check exists to prevent.
    """
    if name in _DRIVERS:
        raise AdapterFactoryError(
            f"driver {name!r} is already registered; a second registration would silently "
            "replace the implementation a registry entry resolves to"
        )
    _DRIVERS[name] = builder


def known_drivers() -> tuple[str, ...]:
    """Every live driver currently registered, sorted. Empty is a legitimate state."""
    return tuple(sorted(_DRIVERS))


@dataclass(frozen=True, slots=True)
class SkippedPlacement:
    """A configured placement that was deliberately not built, and why."""

    sensor_id: str
    registry_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class AdapterSet:
    """What a build produced, including what it deliberately did not.

    `skipped` is not an error list. It is the difference between the site document and what is
    actually running, which the operator interface must be able to show — an absent sensor and a
    sensor that was never configured look identical otherwise.
    """

    adapters: tuple[SensorAdapter, ...] = ()
    skipped: tuple[SkippedPlacement, ...] = ()

    @property
    def running_ids(self) -> frozenset[str]:
        """Which sensors are actually feeding the pipeline.

        The authority on whether a placement is running. Inferring it from the *absence* of a
        skip reason is wrong in the one case that matters: an `AdapterSet` that was never
        populated has no adapters and no skip reasons either, so every sensor would read as
        running while nothing was. Membership answers the question directly.
        """
        return frozenset(adapter.sensor_id for adapter in self.adapters)

    @property
    def live_count(self) -> int:
        """Adapters feeding real data. What `GET /system/mode` reports, and what the startup
        guard checks a LIVE run against."""
        return sum(1 for a in self.adapters if not a.sensor_type.is_simulated)

    @property
    def simulated_count(self) -> int:
        return sum(1 for a in self.adapters if a.sensor_type.is_simulated)


def build_adapters(
    placements: list[Placement],
    registry: dict[str, RegistryEntry],
    *,
    clock: Clock,
    frame: LocalFrame,
    run_mode: RunMode,
    simulation: SimulationContext | None = None,
) -> AdapterSet:
    """Build every placement this run mode calls for, recording the ones it does not.

    Raises rather than skipping when a placement *should* have been built and could not — an
    unknown registry key, an unregistered driver, a driver that rejected its configuration.
    Those are configuration errors, and a system that starts anyway has a coverage gap nobody
    asked for.
    """
    adapters: list[SensorAdapter] = []
    skipped: list[SkippedPlacement] = []

    for placement in placements:
        entry = registry.get(placement.registry_key)
        if entry is None:
            known = ", ".join(sorted(registry)) or "none"
            raise AdapterFactoryError(
                f"sensor {placement.sensor_id!r} references registry key "
                f"{placement.registry_key!r}, which the registry does not define. Known keys: "
                f"{known}"
            )

        reason = _skip_reason(entry, run_mode, simulation)
        if reason is not None:
            skipped.append(
                SkippedPlacement(placement.sensor_id, placement.registry_key, reason)
            )
            continue

        adapters.append(_build_one(placement, entry, clock, frame, simulation))

    return AdapterSet(tuple(adapters), tuple(skipped))


def _skip_reason(
    entry: RegistryEntry, run_mode: RunMode, simulation: SimulationContext | None
) -> str | None:
    """Why this entry is not built in this mode, or None to build it."""
    if isinstance(entry, SensorModel):
        if run_mode is RunMode.LIVE:
            return (
                "simulated sensor, and this deployment is running in LIVE mode; synthetic "
                "detections must not enter a picture presented as real"
            )
        if simulation is None:
            return (
                "simulated sensor with no scenario attached; it has nothing to observe, and a "
                "simulated sensor reporting a permanently empty sky is worse than an absent one"
            )
        return None

    if run_mode is RunMode.SIMULATION:
        return (
            "live sensor, and this deployment is running in SIMULATION mode; every detection on "
            "screen is synthetic and a real source would contradict that"
        )
    return None


def _build_one(
    placement: Placement,
    entry: RegistryEntry,
    clock: Clock,
    frame: LocalFrame,
    simulation: SimulationContext | None,
) -> SensorAdapter:
    if isinstance(entry, SensorModel):
        # `_skip_reason` has already established the context exists; asserting it here would be
        # a second check of the same condition, so the type narrowing is done by the caller's
        # contract rather than restated.
        assert simulation is not None  # noqa: S101 — guaranteed by _skip_reason above
        return SimulatedSensorAdapter(
            clock=clock,
            sensor_id=placement.sensor_id,
            model=entry,
            position_enu=placement.position_enu,
            frame=frame,
            truth_source=simulation.truth_source,
            scenario_seed=simulation.scenario_seed,
            boresight_deg=placement.boresight_deg,
        )

    builder = _DRIVERS.get(entry.driver)
    if builder is None:
        known = ", ".join(known_drivers()) or "none — no live sensor driver is registered"
        raise AdapterFactoryError(
            f"sensor {placement.sensor_id!r} needs driver {entry.driver!r}, which is not "
            f"registered. Registered drivers: {known}"
        )

    adapter = builder(
        LiveBuildRequest(
            clock=clock,
            sensor_id=placement.sensor_id,
            model=entry,
            position_enu=placement.position_enu,
            frame=frame,
            boresight_deg=placement.boresight_deg,
        )
    )

    if adapter.sensor_id != placement.sensor_id:
        raise AdapterFactoryError(
            f"driver {entry.driver!r} built an adapter identifying itself as "
            f"{adapter.sensor_id!r} for placement {placement.sensor_id!r}; the event log, health "
            "and fusion all key on this identifier"
        )
    return adapter


@dataclass(frozen=True, slots=True)
class _ResetToken:
    """Restores the driver table. Test support only — see `driver_registry_reset`."""

    previous: dict[str, DriverBuilder] = field(default_factory=dict)


def driver_registry_reset(token: _ResetToken | None = None) -> _ResetToken:
    """Snapshot or restore the registered drivers.

    Exists because the table is process-global — which is the right shape for a plugin registry
    and the wrong shape for a test suite that registers a stub driver and must not leak it into
    the next test. Called with no argument it snapshots and clears; called with the returned
    token it restores.
    """
    if token is None:
        snapshot = _ResetToken(dict(_DRIVERS))
        _DRIVERS.clear()
        return snapshot

    _DRIVERS.clear()
    _DRIVERS.update(token.previous)
    return token
