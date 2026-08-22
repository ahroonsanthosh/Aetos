"""Runnable entry point: wires the pipeline together and serves the API.

Everything below this line existed already as modules that tests constructed individually.
This is the one place they are assembled into a system that runs, which is what makes the
repository usable rather than merely correct.

    python -m backend.main                          serve the API against an empty log
    python -m backend.main --scenario a_benign_transit   serve, and drive that scenario

The scenario runner here is a **development and demonstration driver**, not the scenario
control API. `POST /scenarios/{id}/start` still reports itself unimplemented, because it is:
starting a scenario over HTTP needs lifecycle, authorisation and concurrency answers this
does not have. What this gives is a way to see the whole pipeline work end to end, which is
the thing the specification asks to be verified before anything is built on top of it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
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
from scenarios.library import load_all
from scenarios.world import ScenarioWorld
from sensors.adapters.factory import (
    AdapterFactoryError,
    AdapterSet,
    Placement,
    SimulationContext,
    build_adapters,
)
from sensors.adapters.interface import SensorAdapter
from sensors.adapters.registry import load_sensor_registry
from sensors.schemas.detection import Detection, SensorType

_LOG = logging.getLogger("aetos.pipeline")

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

        self.events = SqliteEventStore(clock, data_dir / "events.db")
        self.repositories = SqliteRepositories(clock, data_dir / "aetos.db")
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

        self.registry = load_sensor_registry()
        self.sources = AdapterSet()
        """What is actually feeding the pipeline, and what was configured but not built.

        Empty until `attach_sources` is called. The distinction between "no sensors configured"
        and "sensors configured but nothing started" is carried in `AdapterSet.skipped` rather
        than being lost in an empty list."""

        # The site and its sensors are configuration the API reads back, so they are persisted
        # once at startup rather than being loaded independently by every request.
        self.repositories.sites.upsert(geometry.site)
        self._persist_sensors()

    def _persist_sensors(self) -> None:
        """Write the site's sensor placements so `GET /sensors` can read them back.

        Without this the endpoint returns an empty list on a site that plainly has sensors,
        and the operator map draws none — which reads as "this site has no sensors" rather
        than "nobody told the database about them".

        `sensor_type` is now filled in from the registry entry rather than left null. It was
        null because nothing here loaded the registry, so a type would have been a guess; the
        pipeline holds the registry now, so it is a read. That matters more than it looks: the
        type is what tells the interface whether a sensor is simulated or real, and a null one
        left the sensors page unable to say.
        """
        self.repositories.sensors.replace_for_site(
            self.geometry.site.site_id,
            [
                SensorRecord(
                    sensor_id=placement.sensor_id,
                    site_id=self.geometry.site.site_id,
                    registry_key=placement.registry_key,
                    sensor_type=self._registered_type(placement.registry_key),
                    latitude=placement.position.latitude,
                    longitude=placement.position.longitude,
                    height_m=placement.height_m,
                    boresight_deg=placement.boresight_deg,
                    enabled=placement.enabled,
                )
                for placement in self.geometry.site.sensors
            ],
        )

    def _registered_type(self, registry_key: str) -> SensorType | None:
        """The sensor type this key declares, or None if the registry does not define it.

        None rather than raising: a placement naming an unknown key is caught loudly by the
        adapter factory when sources are attached, with a message that names the known keys.
        Failing here instead would refuse to start the API over a configuration problem the
        operator could not then see, which is the worse of the two failures.
        """
        entry = self.registry.get(registry_key)
        return entry.sensor_type if entry is not None else None

    def attach_sources(self, run_mode: RunMode, scenario_id: str | None = None) -> AdapterSet:
        """Build and start the adapters this run mode calls for.

        Replaces the previous `attach_scenario`, which constructed simulated adapters directly
        and had no path that could construct anything else. Which implementation a placement
        resolves to is now the factory's decision, read from the registry — so LIVE and HYBRID
        are configuration rather than code that does not exist.
        """
        simulation: SimulationContext | None = None
        if scenario_id is not None:
            scenario = load_all()[scenario_id]
            simulation = SimulationContext(
                truth_source=ScenarioWorld(scenario, self.clock.now()),
                scenario_seed=scenario.seed,
            )

        positions = self.geometry.site.sensor_positions_enu()
        self.sources = build_adapters(
            [
                Placement(
                    sensor_id=placement.sensor_id,
                    registry_key=placement.registry_key,
                    position_enu=positions[placement.sensor_id],
                    boresight_deg=placement.boresight_deg,
                )
                for placement in self.geometry.site.sensors
                if placement.enabled
            ],
            self.registry,
            clock=self.clock,
            frame=self.geometry.frame,
            run_mode=run_mode,
            simulation=simulation,
        )

        for skip in self.sources.skipped:
            _LOG.info("sensor %s not started: %s", skip.sensor_id, skip.reason)
        for adapter in self.sources.adapters:
            adapter.start()
        return self.sources

    @property
    def _adapters(self) -> tuple[SensorAdapter, ...]:
        return self.sources.adapters

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
            detections.extend(adapter.get_detections())

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

    def close(self) -> None:
        for adapter in self._adapters:
            adapter.stop()
        self.repositories.close()
        self.events.close()


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})
"""Interfaces reachable only from this machine.

`0.0.0.0` is deliberately absent. It is what people reach for meaning "let me at it from my
laptop", and it means every interface including whatever the machine is plugged into.
"""


class StartupRefused(RuntimeError):
    """The requested configuration would expose the system, or would present it dishonestly.

    Raised instead of starting. Both categories share the property that the failure is invisible
    from inside: an exposed console looks like a working one, and a LIVE deployment with nothing
    attached looks exactly like a quiet sky.
    """


def guard_sources(run_mode: RunMode, sources: AdapterSet) -> None:
    """Refuse to start in a configuration that would misrepresent what the operator is seeing.

    Two refusals.

    1. **LIVE with no live adapter.** Every screen would say LIVE while nothing real was
       feeding the pipeline, and an empty picture would read as clear airspace rather than as
       a system that is not connected to anything. This is FM-010 at deployment scale: the
       difference between "nothing is out there" and "nothing is looking" must never be
       invisible, and it is least visible in exactly the mode that claims to be real.

    2. **Nothing at all.** No adapter of any kind was built, so the pipeline is a display with
       no input. Distinct from the first because it is also reachable in SIMULATION — a
       scenario nobody attached — and the operator would see the same confident empty map.

    Deliberately not a refusal: HYBRID or SIMULATION with *some* placements skipped. That is a
    legitimate and common state — a live-fitted site running a training scenario, a bench test
    of one sensor — and `AdapterSet.skipped` carries every one with its reason for the interface
    to show. Refusing it would make the mechanism unusable for the cases it exists to serve.
    """
    if not sources.adapters:
        detail = (
            "; ".join(f"{s.sensor_id}: {s.reason}" for s in sources.skipped)
            or "the site configuration defines no enabled sensors"
        )
        raise StartupRefused(
            f"Refusing to start in {run_mode.value.upper()} mode with no sensor adapters "
            f"running. The operator picture would be a confident empty map that reads as clear "
            f"airspace rather than as a system with no input (FM-010). Reason(s) — {detail}"
        )

    if run_mode is RunMode.LIVE and sources.live_count == 0:
        raise StartupRefused(
            f"Refusing to start in LIVE mode with {sources.simulated_count} simulated adapter(s) "
            "and no real ones. Every screen would say LIVE over synthetic data. Run in "
            "SIMULATION or HYBRID, or attach a real sensor."
        )


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


def build_application(
    site: str = DEFAULT_SITE,
    scenario: str | None = None,
    data_dir: Path | None = None,
    tick_s: float = DEFAULT_TICK_S,
    authenticated: bool = True,
    run_mode: RunMode = RunMode.SIMULATION,
):
    """Assemble the pipeline and the API around it.

    `authenticated` selects the identity provider. True is the default and the only setting safe
    off loopback; False attributes everything to one local operator with no login at all
    (ADR 0008, FM-001) and exists for a single-user machine. `guard_bind` refuses to combine it
    with a network interface.

    `run_mode` decides which kinds of adapter the factory builds, and is reported to the
    interface as a fact the backend asserts rather than a constant the frontend carries. It
    defaults to SIMULATION and, with no live driver registered, LIVE and HYBRID currently refuse
    to start rather than pretending — which is the honest behaviour for a system that has the
    seam but not yet the hardware.
    """
    clock = SystemClock()
    geometry = load_site_geometry(site)
    resolved = data_dir or ConfigPaths.default().repo_root / "data"

    pipeline = Pipeline(clock, geometry, resolved)

    # Sources are attached whenever the mode calls for any, not only when a scenario was named:
    # a LIVE or HYBRID run has real sensors to start with no scenario in sight, and the previous
    # "attach only if a scenario was given" rule would have left them unbuilt.
    #
    # SIMULATION with no scenario is the one case that deliberately skips both. It is
    # `--no-scenario`: an idle system serving the interface over an empty log, which is a
    # documented way to look at the application and not a claim that anything is being watched.
    # `guard_sources` would refuse it for having no adapters, and would be right about a
    # deployment and wrong about this.
    if scenario is not None or run_mode is not RunMode.SIMULATION:
        pipeline.attach_sources(run_mode, scenario)
        guard_sources(run_mode, pipeline.sources)

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
        run_mode=run_mode,
        sources=pipeline.sources,
    )

    app = create_app(services)
    _attach_pipeline_task(app, pipeline, tick_s)

    # After the routers, so the interface's catch-all can only ever match paths the API did
    # not claim.
    mount_frontend(app)
    return app


def _attach_pipeline_task(app, pipeline: Pipeline, tick_s: float) -> None:
    """Run the pipeline on the event loop for as long as the server is up.

    Composed into the application's existing lifespan rather than registered with
    `@app.on_event`. FastAPI **silently ignores** startup and shutdown handlers when a
    lifespan context is supplied, and `create_app` supplies one — so the on_event form
    registers cleanly, runs never, and gives no indication of either.

    The loop runs whenever *any* adapter is attached, rather than only when a scenario was
    named. A LIVE or HYBRID deployment has real sensors and no scenario at all, and the
    scenario-only condition this replaced would have served their detections to nobody: the
    adapters would have been started and never polled.
    """
    existing_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope_app):
        # Enter the API's own lifespan first: it catches the projection up and starts the
        # broadcaster, and the pipeline should not be appending events before the projection
        # has absorbed whatever a previous run left behind.
        async with existing_lifespan(scope_app):
            handle: asyncio.Task | None = None

            if pipeline.sources.adapters:

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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--tick", type=float, default=DEFAULT_TICK_S, help="pipeline cycle interval in seconds"
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--mode",
        default=RunMode.SIMULATION.value,
        choices=[mode.value for mode in RunMode],
        help=(
            "which kinds of sensor adapter to build: 'simulation' builds only simulated "
            "sensors, 'live' only real ones, 'hybrid' both. LIVE and HYBRID need a registry "
            "entry of kind 'live' whose driver is registered, and the process refuses to start "
            "in LIVE mode with nothing real attached."
        ),
    )
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
    try:
        app = build_application(
            site=arguments.site,
            scenario=arguments.scenario,
            data_dir=arguments.data_dir,
            tick_s=arguments.tick,
            authenticated=authenticated,
            run_mode=RunMode(arguments.mode),
        )
    except (StartupRefused, AdapterFactoryError) as refusal:
        # Both are configuration problems with an operator-readable explanation attached, and
        # both are worth failing on rather than starting degraded. A stack trace would bury the
        # sentence that says what to change.
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
