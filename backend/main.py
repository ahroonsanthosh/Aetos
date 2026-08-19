"""Runnable entry point: wires the pipeline together and serves the API.

Everything below this line existed already as modules that tests constructed individually. This
is the one place they are assembled into a system that runs, which is what makes the repository
usable rather than merely correct.

    python -m backend.main                                    idle system, empty log
    python -m backend.main --scenario a_benign_transit        drive the simulator
    python -m backend.main --mode live --site my_site         read this site's real hardware

## What this file decides

**Which sensors feed the pipeline**, from the run mode and the site document, refusing any
combination it cannot honour rather than starting something that would mislead an operator —
a LIVE badge over an empty sky, or synthetic tracks in a live picture with nothing saying so
(`resolve_sources`, ADR 0018).

**Where this deployment's history lives.** `data/<site>/<mode>/`, claimed by a marker file and
held by a lock, so one deployment cannot inherit another's tracks and cannot be written by two
processes at once (`resolve_data_dir`, FM-053).

**Whether it is safe to bind what was asked for.** `guard_bind` refuses to expose an
unauthenticated console, an account-less one, or session cookies in clear (ADR 0016).

## What it is not

The scenario runner here is a **development and demonstration driver**, not the scenario control
API. `POST /scenarios/{id}/start` still reports itself unimplemented, because it is: starting a
scenario over HTTP needs lifecycle, authorisation and concurrency answers this does not have.

`start.sh` is the developer path — it manages a virtualenv and builds the interface. A deployed
service runs this module directly; see `docs/deployment.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

import uvicorn

from backend.alerting.policy import load_alert_policy
from backend.alerting.service import AlertService
from backend.api.app import create_app
from backend.api.identity import LocalOperatorIdentityProvider, SessionIdentityProvider
from backend.api.services import ApiServices
from backend.api.static import mount_frontend
from backend.auth.policy import load_auth_policy
from backend.auth.service import AuthService
from backend.classification.classifier import build_default_classifier
from backend.classification.service import ClassificationService, CycleEvidence
from backend.common.clock import Clock, SystemClock
from backend.common.config import ConfigPaths, RunMode
from backend.database.projection import TrackProjection
from backend.database.records import SensorRecord
from backend.database.sqlite import SqliteRepositories
from backend.events.store import SqliteEventStore
from backend.ingestion.pipeline import IngestionPipeline
from backend.risk.engine import build_risk_engine, load_risk_policy
from backend.risk.service import RiskService
from backend.tracking.track_manager.manager import TrackManager
from backend.zones.geometry import SiteGeometry, load_site_geometry
from backend.zones.site import SensorPlacement, Site
from scenarios.library import load_all
from scenarios.world import ScenarioWorld
from sensors.adapters.interface import SensorAdapter, SensorHealth, SensorState
from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.registry import load_sensor_registry
from sensors.adapters.uart_fmcw_radar import (
    ByteLink,
    DetectionGateConfig,
    MountConfig,
    SerialLibraryMissing,
    UartFmcwRadarAdapter,
    UartFmcwRadarConfig,
    UartLinkConfig,
    available_ports,
)
from sensors.schemas.detection import Detection, SensorType

_LOG = logging.getLogger("aetos.pipeline")

DRIVER_SENSOR_TYPES = {"uart_fmcw_radar": SensorType.RADAR}
"""What kind of source each hardware driver is, for the sensors the API serves.

Asserted here rather than left null: the driver declares its own `sensor_type` to every
detection it emits, so leaving the sensors table blank would have the same system describing
one sensor two ways. Generic kinds only — no manufacturer or model appears in any of it."""

DEFAULT_SITE = "fictional_test_range"
DEFAULT_TICK_S = 0.5


class Pipeline:
    """Simulator to tracker to event log, stepped on a fixed cadence.

    Holds the pieces the API reads from, so a caller assembles one object rather than six and
    the wiring lives in one place instead of being re-derived at every call site.
    """

    def __init__(
        self,
        clock: Clock,
        geometry: SiteGeometry,
        data_dir: Path,
    ) -> None:
        self.clock = clock
        self.geometry = geometry
        self.data_dir = data_dir

        self.events = SqliteEventStore(clock, data_dir / "events.db")
        self.repositories = SqliteRepositories(
            clock, data_dir / "aetos.db", identity_path=ConfigPaths.default().identity_db
        )
        self.projection = TrackProjection(
            store=self.events,
            tracks=self.repositories.tracks,
            history=self.repositories.track_history,
            state=self.repositories.projection_state,
        )
        self.tracker = TrackManager(clock, self.events, geometry.frame)
        self.ingestion = IngestionPipeline(clock, self.events, geometry)
        self.classification = ClassificationService(
            build_default_classifier(), self.events, sink=self.tracker
        )
        self.risk = RiskService(
            build_risk_engine(),
            load_risk_policy(),
            geometry,
            self.events,
            sink=self.tracker,
        )
        self.auth = AuthService(
            load_auth_policy(),
            clock,
            self.repositories.users,
            self.repositories.credentials,
            self.repositories.sessions,
            self.events,
        )
        self.alerting = AlertService(load_alert_policy(), self.events)

        # Alerts outlive the process that raised them. Rebuilding from the log before the
        # first cycle is what makes that true — without it a restart discharges every open
        # alert, and an operator cannot tell an empty feed from a quiet sky.
        self.alerting.rebuild()

        self._adapters: list[SensorAdapter] = []

        self._poll_failures: dict[str, str] = {}
        """Sensor id to the error its last poll raised, cleared when it polls cleanly. Read by
        `sensor_health()` so a driver that throws cannot report itself healthy."""

        self.deployment_lock: DeploymentLock | None = None
        """Held by whoever assembled this pipeline, released when it closes. None in tests and
        embedded uses, which construct a Pipeline directly against a directory of their own."""

        # The site and its sensors are configuration the API reads back, so they are persisted
        # once at startup rather than being loaded independently by every request.
        self.repositories.sites.upsert(geometry.site)
        self._persist_sensors()

    def _persist_sensors(self) -> None:
        """Write the site's sensor placements so `GET /sensors` can read them back.

        Without this the endpoint returns an empty list on a site that plainly has sensors,
        and the operator map draws none — which reads as "this site has no sensors" rather
        than "nobody told the database about them".
        """
        self.repositories.sensors.replace_for_site(
            self.geometry.site.site_id,
            [
                SensorRecord(
                    sensor_id=placement.sensor_id,
                    site_id=self.geometry.site.site_id,
                    registry_key=placement.registry_key,
                    hardware_driver=placement.hardware.driver if placement.hardware else None,
                    sensor_type=(
                        DRIVER_SENSOR_TYPES.get(placement.hardware.driver)
                        if placement.hardware
                        else None
                    ),
                    latitude=placement.position.latitude,
                    longitude=placement.position.longitude,
                    height_m=placement.height_m,
                    boresight_deg=placement.boresight_deg,
                    enabled=placement.enabled,
                )
                for placement in self.geometry.site.sensors
            ],
        )

    @property
    def adapters(self) -> list[SensorAdapter]:
        return list(self._adapters)

    @property
    def live_adapter_count(self) -> int:
        """How many attached adapters read physical hardware.

        The number `GET /system/mode` reports, and therefore the number the operator interface
        turns into the SIMULATION-or-LIVE badge. Counted from the adapters actually attached,
        not from what the configuration asked for, because a live sensor that failed to attach
        must not leave the display claiming to be live.
        """
        return sum(1 for adapter in self._adapters if not adapter.sensor_type.is_simulated)

    def sensor_health(self) -> list[SensorHealth]:
        """Every attached adapter's self-report, for `GET /system/health`.

        Polled per request rather than cached: the whole value of the row is that it is current
        when someone looks at it, which is exactly when they have stopped trusting the display.

        Two things are overridden rather than passed through. An adapter that raised on its
        last poll is reported DEGRADED whatever it says about itself — a driver that throws and
        then claims OK is describing an intention, not a state. And an adapter whose `health()`
        itself raises is reported OFFLINE rather than being allowed to take the health endpoint
        down with it, because the one moment this endpoint must answer is when something is
        already wrong.
        """
        reports: list[SensorHealth] = []
        for adapter in self._adapters:
            try:
                health = adapter.health()
            except Exception as exc:  # noqa: BLE001 — see the docstring
                reports.append(
                    SensorHealth(
                        sensor_id=adapter.sensor_id,
                        sensor_type=adapter.sensor_type,
                        state=SensorState.OFFLINE,
                        reason=f"health() raised {type(exc).__name__}: {exc}",
                    )
                )
                continue

            failure = self._poll_failures.get(adapter.sensor_id)
            if failure is not None and health.state is not SensorState.OFFLINE:
                health = health.model_copy(
                    update={
                        "state": SensorState.DEGRADED,
                        "reason": f"last poll raised {failure}",
                    }
                )
            reports.append(health)
        return reports

    def attach_scenario(self, scenario_id: str) -> None:
        """Point the simulated sensors at a scenario's world.

        Simulated placements only. A live placement is not driven by a scenario — the sky is —
        so running a scenario at a site that has both leaves the hardware reporting whatever it
        actually sees, alongside synthetic tracks. That is HYBRID mode, and it is why every
        detection carries its sensor's type.
        """
        scenario = load_all()[scenario_id]
        registry = load_sensor_registry()
        world = ScenarioWorld(scenario, self.clock.now())
        positions = self.geometry.site.sensor_positions_enu()

        adapters = [
            SimulatedSensorAdapter(
                clock=self.clock,
                sensor_id=placement.sensor_id,
                model=registry[placement.registry_key],
                position_enu=positions[placement.sensor_id],
                frame=self.geometry.frame,
                truth_source=world,
                scenario_seed=scenario.seed,
                boresight_deg=placement.boresight_deg,
            )
            for placement in self.geometry.site.sensors
            if placement.enabled and placement.registry_key is not None
        ]
        self._attach(adapters)

    def attach_hardware(self, link_factory: LinkFactory | None = None) -> None:
        """Construct and start an adapter for every enabled real sensor at this site.

        Construction cannot fail on an unplugged device — adapters open nothing until they are
        started, and a start that cannot reach its hardware records OFFLINE rather than raising.
        A site with three radars and one unplugged USB cable therefore runs with three, and says
        which one is missing, instead of refusing to start at all.

        `link_factory` replaces the serial port with something else, which is how the whole
        live path — bytes in, tracks out — is tested without hardware. It is the same seam the
        adapter itself offers, carried up to here so that the *wiring* is covered too and not
        just the driver.
        """
        self._attach(
            [
                build_hardware_adapter(
                    self.clock,
                    placement,
                    self.data_dir,
                    self.geometry.site.reference.altitude_m,
                    link_factory=link_factory,
                )
                for placement in self.geometry.site.sensors
                if placement.enabled and placement.is_live
            ]
        )

    def _attach(self, adapters: list[SensorAdapter]) -> None:
        for adapter in adapters:
            adapter.start()
        self._adapters.extend(adapters)

    def step(self) -> int:
        """Run one cycle. Returns how many detections were accepted.

        The order is the pipeline in `docs/architecture.md`: sensors, ingestion, tracking,
        classification, risk, alerting, projection. Each stage sees only what the one before
        it produced — risk runs after classification because identity is one of its inputs,
        and a track assessed before it was classified would be scored on geometry alone for
        one cycle longer than necessary.
        """
        detections: list[Detection] = []
        for adapter in self._adapters:
            detections.extend(self._poll(adapter))

        # Validates, deduplicates, records DETECTION_RECEIVED, and reduces to the tracker's
        # input. The classification hints come back separately — they are evidence about what
        # a track is, and must not reach the filter deciding where it is (ADR 0002).
        ingested = self.ingestion.ingest(detections)

        tracks = self.tracker.process(ingested.for_tracking)

        classified = self.classification.classify_cycle(
            tracks,
            CycleEvidence.from_assignments(
                self.tracker.last_assignments, ingested.hints, ingested.quality
            ),
        )

        now = self.clock.now()
        assessed = self.risk.assess_cycle(classified, now)

        # Last, and reading the assessed tracks rather than the risk engine. Alerting decides
        # only whether a level warrants interrupting a human; it has no opinion on how that
        # level was reached, and taking its input from `track.threat` is what keeps it that
        # way (ADR 0015).
        self.alerting.review_cycle(assessed, now)

        # Catch the projection up after tracking, never before: the projection reads the log,
        # and reading it before this cycle's events are appended would leave the API serving a
        # picture one cycle stale for no reason.
        self.projection.catch_up()
        return ingested.accepted

    def _poll(self, adapter: SensorAdapter) -> list[Detection]:
        """One adapter's detections, with its failures isolated from every other sensor's.

        The cycle used to poll adapters in a bare loop, so one misbehaving driver raised
        through `step()` and cost *every* sensor its cycle. The task-level guard kept the run
        alive, but the operator picture stopped advancing on all sensors because one was
        faulty — and the reason was in a log rather than on the display.

        Start-up already tolerates this: a site with three radars and one unplugged cable runs
        with three. Runtime now agrees with start-up. The failure is not swallowed — it is
        recorded, logged, and reported through `sensor_health()`, which is where an operator
        looks when the picture seems thin.
        """
        try:
            detections = adapter.get_detections()
        except Exception as exc:  # noqa: BLE001 — an adapter may raise anything; none of it
            # is worth the rest of the site's cycle.
            message = f"{type(exc).__name__}: {exc}"
            first = self._poll_failures.get(adapter.sensor_id) != message
            self._poll_failures[adapter.sensor_id] = message
            if first:
                _LOG.exception("Sensor %s failed to poll and was skipped.", adapter.sensor_id)
            return []

        self._poll_failures.pop(adapter.sensor_id, None)
        return detections

    def close(self) -> None:
        for adapter in self._adapters:
            adapter.stop()
        self.repositories.close()
        self.events.close()
        if self.deployment_lock is not None:
            self.deployment_lock.release()


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})
"""Interfaces reachable only from this machine.

`0.0.0.0` is deliberately absent. It is what people reach for meaning "let me at it from my
laptop", and it means every interface including whatever the machine is plugged into.
"""


LinkFactory = Callable[[UartLinkConfig], ByteLink]
"""How to obtain a byte pipe for a link configuration. Defaults to a real serial port."""


def build_hardware_adapter(
    clock: Clock,
    placement: SensorPlacement,
    data_dir: Path,
    reference_altitude_m: float = 0.0,
    link_factory: LinkFactory | None = None,
) -> SensorAdapter:
    """One real sensor, from its site placement.

    This is the only place in the repository where a site document meets a hardware driver, and
    it is deliberately dull: it copies configuration across and constructs one object. Anything
    cleverer — deriving mount tilt from a zone, defaulting a port from a scan of /dev — would
    put a guess between what the installer wrote down and what the sensor is told it is.

    Lives in `backend/main.py` rather than in `sensors/` so that the adapter package keeps its
    dependency rule: adapters know about the canonical schema and the clock, and nothing about
    sites, zones or run modes.
    """
    link = placement.hardware
    if link is None:
        raise ValueError(f"sensor {placement.sensor_id} has no hardware link to build")

    if link.driver != "uart_fmcw_radar":  # pragma: no cover - the model's Literal forbids it
        raise ValueError(f"unknown sensor driver {link.driver!r}")

    config = UartFmcwRadarConfig(
        sensor_id=placement.sensor_id,
        link=UartLinkConfig(port=link.port, baud=link.baud),
        mount=MountConfig(
            latitude=placement.position.latitude,
            longitude=placement.position.longitude,
            # `height_m` is height above the *site reference*, and the driver works in
            # metres above the WGS-84 ellipsoid like everything else — so the reference
            # altitude has to be added back. Skipping this is a silent vertical offset of
            # exactly the site's elevation on every detection (FM-004).
            altitude_m=reference_altitude_m + placement.height_m,
            boresight_deg=placement.boresight_deg,
            tilt_deg=link.tilt_deg,
        ),
        gate=DetectionGateConfig(
            min_range_m=link.min_range_m, max_range_m=link.max_range_m
        ),
        raw_log_path=(data_dir / link.raw_log_path) if link.raw_log_path else None,
    )
    return UartFmcwRadarAdapter(
        clock, config, link=link_factory(config.link) if link_factory else None
    )


def _warn_if_reads_could_stall_the_cycle(pipeline: Pipeline, tick_s: float) -> None:
    """Say so when the serial reads could take up most of the pipeline cycle.

    Each live adapter's read blocks up to its configured timeout on a quiet port, on the event
    loop thread. Enough sensors, or a generous timeout, and the loop spends its time waiting —
    the interface goes sluggish and the cause is invisible from the outside. Cheap to detect at
    start-up, miserable to diagnose later.
    """
    budget = sum(
        adapter.read_timeout_s
        for adapter in pipeline.adapters
        if isinstance(adapter, UartFmcwRadarAdapter)
    )
    if budget > tick_s / 2:
        _LOG.warning(
            "Serial reads may block up to %.2fs of each %.2fs pipeline cycle. The interface "
            "will feel sluggish and the stream will jitter. Lower read_timeout_s, raise --tick, "
            "or fit fewer sensors per process.",
            budget,
            tick_s,
        )


def _report_hardware_state(pipeline: Pipeline, tick_s: float) -> None:
    """Log what each real sensor said about itself immediately after starting.

    Loudly, and at start-up rather than on request, because the two ways live operation goes
    wrong are both silent: a sensor that never opened its port, and a sensor whose byte stream
    never syncs. Both produce an empty display that looks exactly like a quiet sky, and this is
    the moment at which the difference is cheapest to notice.

    The caveats are logged with it. This hardware is unvalidated (FM-050, FM-052) and the
    operator should be told so by the system rather than by a document they may not have read.
    """
    offline = False
    for adapter in pipeline.adapters:
        if adapter.sensor_type.is_simulated:
            continue
        health = adapter.health()
        log = _LOG.info if health.state is SensorState.OK else _LOG.warning
        log("Sensor %s: %s — %s", health.sensor_id, health.state.upper(), health.reason)
        offline = offline or health.state is SensorState.OFFLINE

    if offline:
        _report_available_ports()

    _warn_if_reads_could_stall_the_cycle(pipeline, tick_s)

    if pipeline.live_adapter_count:
        _LOG.warning(
            "LIVE mode: %d real sensor(s) attached. This hardware is unvalidated — its frame "
            "layout and baud rate are unconfirmed (FM-050) and its ability to detect a small "
            "airframe at any range is unmeasured (FM-052). Treat every track as evidence to be "
            "checked, not as fact. See docs/live_mode.md.",
            pipeline.live_adapter_count,
        )


def _report_available_ports() -> None:
    """List the serial ports this machine has, when a configured one could not be opened.

    "Could not open COM4" leaves someone in Device Manager guessing. "Could not open COM4, but
    here is COM7 with a CP2102 on it" ends the problem on the line it appeared. The listing is
    diagnostic help, so a failure to produce it is reported and never raised — a machine that
    cannot enumerate its ports still has a system to run.
    """
    try:
        ports = available_ports()
    except SerialLibraryMissing as exc:
        _LOG.warning("%s", exc)
        return
    except Exception as exc:  # pragma: no cover - defensive: enumeration is best-effort
        _LOG.warning("Could not list this machine's serial ports: %s", exc)
        return

    if ports:
        _LOG.warning(
            "Serial ports this machine reports: %s. Set the right one in the site's hardware "
            "block (it will usually name CP2102, CH340 or FTDI).",
            "; ".join(ports),
        )
    else:
        _LOG.warning(
            "This machine reports no serial ports at all. The USB-to-TTL adapter is not "
            "plugged in, or its driver is not installed — see docs/live_mode.md."
        )


class ModeRefused(RuntimeError):
    """The requested run mode cannot be satisfied by this site's configuration.

    Distinct from `StartupRefused`, which is about exposure. This one is about honesty: a
    system told to run LIVE and started anyway with nothing but simulated sensors would show an
    operator synthetic tracks under a LIVE badge, which is the single worst thing this codebase
    can do.
    """


def resolve_sources(
    mode: RunMode, site: Site, scenario: str | None
) -> tuple[bool, str | None]:
    """Which adapter families a run mode wants, and the refusals if it cannot have them.

    Returns `(attach_hardware, scenario_to_run)`. Every refusal below is a state that would
    otherwise produce a confident display over data that is not what it claims to be.
    """
    live_placements = [p for p in site.sensors if p.enabled and p.is_live]
    simulated_placements = [p for p in site.sensors if p.enabled and not p.is_live]

    if mode is RunMode.SIMULATION:
        if scenario is None:
            return False, None
        if not simulated_placements:
            raise ModeRefused(
                f"site {site.site_id!r} has no enabled simulated sensors, so scenario "
                f"{scenario!r} would drive nothing and the display would stay empty for a "
                "reason nobody could see. Add a placement with a registry_key, or run "
                "--mode live against the hardware this site is fitted with."
            )
        return False, scenario

    if mode is RunMode.LIVE:
        if scenario is not None:
            raise ModeRefused(
                "--mode live does not take a scenario. Synthetic tracks alongside real ones is "
                "HYBRID mode; ask for it by name (--mode hybrid) so that whoever reads the "
                "screen knows both are present."
            )
        if not live_placements:
            raise ModeRefused(
                f"--mode live, but site {site.site_id!r} has no enabled sensor with a hardware "
                "block. Nothing would feed the pipeline and the interface would show an empty "
                "sky indistinguishable from a quiet one. Add a hardware placement to "
                f"config/sites/{site.site_id}.json — see docs/live_mode.md."
            )
        return True, None

    # HYBRID: both, and it must genuinely be both or the name is a lie.
    if not live_placements:
        raise ModeRefused(
            f"--mode hybrid, but site {site.site_id!r} has no enabled hardware placement. That "
            "is simulation with a different label; run --mode simulation."
        )
    if scenario is None:
        raise ModeRefused(
            "--mode hybrid needs a scenario to supply the simulated half. Without one it is "
            "live operation with a misleading badge; run --mode live."
        )
    return True, scenario


class StartupRefused(RuntimeError):
    """The requested configuration would expose the system. Raised instead of starting."""


def guard_bind(host: str, *, authenticated: bool, behind_proxy: bool, user_count: int) -> None:
    """Refuse to start in a configuration that would put an open console on a network.

    Three refusals, each for a state that is survivable on a laptop and not survivable on a
    site:

    1. **No authentication, off loopback.** The FM-001 state. Anyone who can route to the port
       gets the full operator interface and the complete event log.
    2. **No accounts, off loopback.** Authentication is configured but nobody can log in, and an
       unusable console is the kind of thing that gets "fixed" at 2am with --no-auth.
    3. **Off loopback without --behind-proxy.** Session cookies would cross the network in
       clear. `--behind-proxy` is an explicit assertion by whoever starts the process that TLS
       is terminated in front of it; it is not inferred from a header, because headers are
       supplied by whoever is connecting.

    A guard rather than a documented warning, because documentation does not run. This is the
    one place the deployment posture is enforced instead of described.
    """
    if host in LOOPBACK_HOSTS:
        return

    if not authenticated:
        raise StartupRefused(
            f"Refusing to bind {host} with authentication disabled. Anyone who can reach this "
            "port would have the full operator interface and the complete event log "
            "(docs/failure_modes.md FM-001). Drop --no-auth, or bind 127.0.0.1."
        )

    if user_count == 0:
        raise StartupRefused(
            f"Refusing to bind {host} with no accounts: nobody could log in. Create one "
            f"first:\n    {sys.executable} -m backend.auth.admin add --username <name> "
            "--role admin"
        )

    if not behind_proxy:
        raise StartupRefused(
            f"Refusing to bind {host} without --behind-proxy. Aetos does not terminate TLS, so "
            "session cookies would cross the network in clear. Put a reverse proxy in front of "
            "it, terminate TLS there, and pass --behind-proxy to confirm you have. See "
            "docs/deployment.md."
        )


class DeploymentLock:
    """An advisory lock held for as long as one process owns a deployment directory.

    Two Aetos processes writing one event log breaks the single-writer discipline the log
    depends on (ADR 0006): interleaved sequence numbers, a projection catching up on events
    from two runs, and no way afterwards to say which process wrote what. Different ports do
    not help — the port stops two servers, not two writers.

    **It fails open.** If the platform cannot lock, or locking errors for any reason, the run
    continues with a warning. The protection is worth having where it works; it is not worth a
    system that refuses to start because its own lock mechanism misbehaved on a machine nobody
    could test. The Windows path in particular is written from documentation rather than from a
    machine, and that is exactly why it must not be able to block a start.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> bool:
        """True when the lock is held by this process, False when another process holds it."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a+")
        except OSError as exc:  # pragma: no cover - unwritable directory is reported elsewhere
            _LOG.warning("Could not open the deployment lock %s: %s", self._path, exc)
            return True

        try:
            if os.name == "nt":  # pragma: no cover - not exercised on this platform
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        except Exception as exc:  # pragma: no cover - defensive: never block a start
            _LOG.warning("Deployment locking is unavailable (%s); continuing without it.", exc)
            handle.close()
            return True

        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()  # closing releases both flock and msvcrt locks
        except OSError:  # pragma: no cover - releasing a dead handle is not an error
            pass


DEPLOYMENT_MARKER = "deployment.json"
DEPLOYMENT_LOCK = "deployment.lock"
"""Names which (site, run mode) a data directory belongs to. See `claim_data_dir`."""


def resolve_data_dir(site: str, mode: RunMode, override: Path | None = None) -> Path:
    """Where this run's event log and projection live.

    **Partitioned by site and run mode**, because persistent state is the record of one
    deployment and mixing two of them in one log is not a storage detail — it is how synthetic
    tracks end up on a live display.

    That is not hypothetical. Before this, a simulation run and a live run shared `data/`: the
    projection survives restarts by design (ADR 0006, FM-030), so starting `--mode live` after a
    scenario inherited that scenario's tracks and served them under a LIVE badge. The badge was
    correct — real adapters *were* attached — and the tracks beside it were synthetic. Nothing
    in the interface could have told the operator, because nothing in the data said so.

    An explicit `--data-dir` is honoured exactly as given, on the principle that naming a
    directory means that directory. `claim_data_dir` then does the checking, so the guarantee
    holds however the path was chosen.
    """
    if override is not None:
        return override
    return ConfigPaths.default().repo_root / "data" / site / mode.value


def claim_data_dir(data_dir: Path, site: str, mode: RunMode) -> None:
    """Record which deployment owns this directory, or refuse if another one already does.

    The partitioning above makes a collision unlikely; this makes it impossible. A directory
    that already holds one deployment's history will not silently accept another's, whether it
    was reached by default or named with `--data-dir`.

    Refusing is the conservative answer only in appearance. The alternative is a system that
    starts happily and shows an operator a picture assembled from two different deployments,
    which is the failure mode this project exists to rule out.
    """
    marker = data_dir / DEPLOYMENT_MARKER
    claim = {"site": site, "mode": mode.value, "schema_version": "1.0.0"}

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StartupRefused(
            f"Could not create the data directory {data_dir}: {exc}. Aetos cannot run without "
            "somewhere to write its event log — a system that keeps no record of what it "
            "showed an operator is worse than one that does not start."
        ) from exc

    if marker.exists():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StartupRefused(
                f"{marker} exists but could not be read ({exc}). It records which deployment "
                "owns this data directory; refusing to start rather than risk mixing two "
                "deployments' history. Delete the directory to start fresh, or point "
                "--data-dir somewhere else."
            ) from exc

        if (existing.get("site"), existing.get("mode")) != (site, mode.value):
            raise StartupRefused(
                f"{data_dir} holds the history of site {existing.get('site')!r} in "
                f"{existing.get('mode')!r} mode, and this run is site {site!r} in "
                f"{mode.value!r} mode.\n\n"
                "  Refusing to start: the event log survives restarts by design, so this run "
                "would inherit the other deployment's tracks and show them as its own — "
                "synthetic tracks on a live display, or one site's picture at another.\n\n"
                "  Use --data-dir to give this run its own directory, or delete that one."
            )
        return

    try:
        marker.write_text(json.dumps(claim, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise StartupRefused(
            f"Could not write {marker}: {exc}. Without it nothing records which deployment owns "
            "this directory, and the next run could inherit this one's history (FM-053)."
        ) from exc


def _warn_about_legacy_data(paths: ConfigPaths) -> None:
    """Point at a pre-partitioning event log rather than leaving it silently orphaned.

    Anyone who ran Aetos before this change has history in `data/events.db`. It is not read any
    more, and the alert-rebuild-on-start that depends on it will find nothing. Saying so once is
    the difference between a puzzling empty feed and a known consequence of an upgrade.
    """
    legacy_log = paths.repo_root / "data" / "events.db"
    if legacy_log.exists():
        _LOG.info(
            "An event log from before per-deployment data directories exists at %s. It is no "
            "longer read: history is now kept per site and run mode. Nothing has been deleted.",
            legacy_log,
        )

    legacy_accounts = paths.repo_root / "data" / "aetos.db"
    if legacy_accounts.exists() and not paths.identity_db.exists():
        _LOG.warning(
            "Accounts now live in %s, shared by every deployment. Any account in the older %s "
            "is not read — create yours again with: %s -m backend.auth.admin add --username "
            "<name> --role admin",
            paths.identity_db,
            legacy_accounts,
            sys.executable,
        )


def build_application(
    site: str = DEFAULT_SITE,
    scenario: str | None = None,
    data_dir: Path | None = None,
    tick_s: float = DEFAULT_TICK_S,
    authenticated: bool = True,
    mode: RunMode = RunMode.SIMULATION,
):
    """Assemble the pipeline and the API around it.

    `mode` selects which adapter families feed the pipeline, and is checked against the site's
    configuration before anything starts (`resolve_sources`). It is not a decoration on the
    display: SIMULATION attaches simulated adapters only, LIVE attaches hardware only, HYBRID
    attaches both. Nothing downstream of ingestion is aware of which one is running.

    `authenticated` selects the identity provider. True is the default and the only setting safe
    off loopback; False attributes everything to one local operator with no login at all
    (ADR 0008, FM-001) and exists for a single-user machine. `guard_bind` refuses to combine it
    with a network interface.
    """
    clock = SystemClock()
    geometry = load_site_geometry(site)

    attach_hardware, scenario = resolve_sources(mode, geometry.site, scenario)

    resolved = resolve_data_dir(site, mode, data_dir)
    claim_data_dir(resolved, site, mode)

    lock = DeploymentLock(resolved / DEPLOYMENT_LOCK)
    if not lock.acquire():
        raise StartupRefused(
            f"Another Aetos process is already running this deployment ({site} in {mode.value} "
            f"mode, {resolved}).\n\n"
            "  Two processes writing one event log interleave their records and leave no way "
            "to tell afterwards which wrote what (ADR 0006).\n\n"
            "  Stop the other one, or give this run its own site or --data-dir."
        )
    _warn_about_legacy_data(ConfigPaths.default())

    pipeline = Pipeline(clock, geometry, resolved)
    pipeline.deployment_lock = lock
    if attach_hardware:
        pipeline.attach_hardware()
        _report_hardware_state(pipeline, tick_s)
    if scenario:
        pipeline.attach_scenario(scenario)

    services = ApiServices(
        clock=clock,
        events=pipeline.events,
        sites=pipeline.repositories.sites,
        sensors=pipeline.repositories.sensors,
        scenarios=pipeline.repositories.scenarios,
        tracks=pipeline.repositories.tracks,
        track_history=pipeline.repositories.track_history,
        projection=pipeline.projection,
        identity=(
            SessionIdentityProvider(clock, pipeline.auth)
            if authenticated
            else LocalOperatorIdentityProvider(clock)
        ),
        auth=pipeline.auth if authenticated else None,
        alerts=pipeline.alerting,
        classification_running=True,
        situation=pipeline.risk,
        run_mode=mode,
        live_adapter_count=pipeline.live_adapter_count,
        sensor_health=pipeline,
    )

    app = create_app(services)
    _attach_pipeline_task(app, pipeline, tick_s)

    # After the routers, so the interface's catch-all can only ever match paths the API did
    # not claim.
    mount_frontend(app)
    return app


def _attach_pipeline_task(app, pipeline: Pipeline, tick_s: float) -> None:
    """Run the pipeline on the event loop for as long as the server is up.

    Stepped whenever *any* adapter is attached, not only when a scenario is running. That
    distinction is what LIVE mode turns on: a live sensor produces detections because there is
    something in the sky, and a loop that only ran for scenarios would leave real hardware
    reporting into a pipeline nobody was stepping.

    Composed into the application's existing lifespan rather than registered with
    `@app.on_event`. FastAPI **silently ignores** startup and shutdown handlers when a
    lifespan context is supplied, and `create_app` supplies one — so the on_event form
    registers cleanly, runs never, and gives no indication of either.
    """
    existing_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope_app):
        # Enter the API's own lifespan first: it catches the projection up and starts the
        # broadcaster, and the pipeline should not be appending events before the projection
        # has absorbed whatever a previous run left behind.
        async with existing_lifespan(scope_app):
            handle: asyncio.Task | None = None

            if pipeline.adapters:

                async def run() -> None:
                    consecutive_failures = 0

                    while True:
                        try:
                            # The tracker and the store are synchronous and are the only
                            # writers, so this runs on the loop thread rather than in an
                            # executor. That keeps the single-writer discipline (ADR 0006)
                            # true by construction rather than by agreement between two
                            # threads.
                            pipeline.step()
                            consecutive_failures = 0
                        except Exception:
                            # An unhandled exception here used to kill this task outright.
                            # The server went on serving, the interface went on reporting a
                            # live stream, and nothing was being tracked — a confident
                            # display over dead data, which is the failure this whole system
                            # is built to avoid. It is now logged loudly and the loop
                            # continues, so a transient fault costs one cycle rather than
                            # the run.
                            consecutive_failures += 1
                            _LOG.exception(
                                "Pipeline cycle failed (%d consecutive). The operator "
                                "picture is not advancing.",
                                consecutive_failures,
                            )

                        await asyncio.sleep(tick_s)

                handle = asyncio.create_task(run())

            try:
                yield
            finally:
                if handle is not None:
                    handle.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await handle
                pipeline.close()

    app.router.lifespan_context = lifespan


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Aetos backend.")
    parser.add_argument("--site", default=DEFAULT_SITE, help="site configuration to load")
    parser.add_argument(
        "--scenario",
        default=None,
        help="scenario to drive the simulator with; omit to serve an idle system",
    )
    parser.add_argument(
        "--mode",
        type=RunMode,
        choices=list(RunMode),
        default=RunMode.SIMULATION,
        help=(
            "where detections come from: simulation (synthetic only), live (this site's "
            "configured hardware only), or hybrid (both, and it must genuinely be both)"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--tick", type=float, default=DEFAULT_TICK_S, help="pipeline cycle interval in seconds"
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--behind-proxy",
        action="store_true",
        help=(
            "assert that a reverse proxy in front of this process terminates TLS. Required to "
            "bind anything but loopback. Also trusts X-Forwarded-* from that proxy."
        ),
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help=(
            "disable authentication entirely; every action is attributed to one local operator "
            "(FM-001). Loopback only — the process refuses to start otherwise."
        ),
    )
    arguments = parser.parse_args()

    authenticated = not arguments.no_auth
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        app = build_application(
            site=arguments.site,
            scenario=arguments.scenario,
            data_dir=arguments.data_dir,
            tick_s=arguments.tick,
            authenticated=authenticated,
            mode=arguments.mode,
        )
    except (ModeRefused, StartupRefused) as refusal:
        raise SystemExit(f"\n  {refusal}\n") from refusal

    # After the application is built, so the account count is a real read of the database rather
    # than a guess, and before uvicorn binds anything.
    try:
        guard_bind(
            arguments.host,
            authenticated=authenticated,
            behind_proxy=arguments.behind_proxy,
            user_count=app.state.services.auth.user_count() if authenticated else 0,
        )
    except StartupRefused as refusal:
        raise SystemExit(f"\n  {refusal}\n") from refusal

    uvicorn.run(
        app,
        host=arguments.host,
        port=arguments.port,
        log_level="info",
        # Only when an operator has asserted a proxy is in front. Trusting X-Forwarded-Proto
        # unconditionally would let any client claim its plain-http request arrived over TLS,
        # which is exactly the claim the Secure cookie decision turns on.
        proxy_headers=arguments.behind_proxy,
        forwarded_allow_ips="*" if arguments.behind_proxy else None,
    )


if __name__ == "__main__":
    main()
