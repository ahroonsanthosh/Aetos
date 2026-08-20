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
from sensors.adapters.interface import SensorAdapter
from sensors.adapters.radar.adapter import UartRadarAdapter
from sensors.adapters.radar.config import hardware_keys, load_radar_hardware
from sensors.adapters.radar.transport import SerialTransport, TransportError
from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.registry import load_sensor_registry
from sensors.schemas.detection import Detection

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

        self._adapters: list[SensorAdapter] = []

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
                    latitude=placement.position.latitude,
                    longitude=placement.position.longitude,
                    height_m=placement.height_m,
                    boresight_deg=placement.boresight_deg,
                    enabled=placement.enabled,
                )
                for placement in self.geometry.site.sensors
            ],
        )

    def attach_scenario(self, scenario_id: str) -> None:
        """Point the simulated sensors at a scenario's world."""
        scenario = load_all()[scenario_id]
        registry = load_sensor_registry()
        world = ScenarioWorld(scenario, self.clock.now())
        positions = self.geometry.site.sensor_positions_enu()

        self._adapters = [
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
            if placement.enabled
        ]
        for adapter in self._adapters:
            adapter.start()

    def attach_live_sensors(self, port_override: str | None = None) -> int:
        """Open every physically fitted sensor this site declares. Returns how many started.

        A placement is *fitted* when its `registry_key` names a document in `config/hardware/`
        rather than a profile in `config/sensor_registry/`. That is the whole of the
        simulated-versus-real decision, and it is made from configuration rather than from a
        flag, so a site cannot be running a real sensor while the configuration a reviewer
        reads says otherwise.

        Failures propagate. A port that will not open is not a sensor watching an empty sky,
        and starting the system with one silently absent would put an operator in front of a
        display showing exactly what a working sensor sees over an empty field (FM-010).
        """
        fitted = hardware_keys()
        placements = [
            placement
            for placement in self.geometry.site.sensors
            if placement.enabled and placement.registry_key in fitted
        ]
        if not placements:
            raise StartupRefused(
                f"site {self.geometry.site.site_id!r} declares no fitted sensors: no enabled "
                f"placement names a document in config/hardware/. Configured keys there are "
                f"{sorted(fitted) or 'none'}."
            )

        positions = self.geometry.site.sensor_positions_enu()
        # Kept alongside its port so a failure names the port that failed rather than whichever
        # one the loop happened to end on.
        adapters: list[tuple[SensorAdapter, str]] = []
        for placement in placements:
            config = load_radar_hardware(placement.registry_key)
            port = port_override or config.link.port
            adapters.append((
                UartRadarAdapter(
                    clock=self.clock,
                    sensor_id=placement.sensor_id,
                    config=config,
                    transport=SerialTransport(
                        port=port,
                        baud_rate=config.link.baud_rate,
                        read_timeout_s=config.link.read_timeout_s,
                    ),
                    position_enu=positions[placement.sensor_id],
                    frame=self.geometry.frame,
                    boresight_deg=placement.boresight_deg,
                ),
                port,
            ))

        for adapter, port in adapters:
            try:
                adapter.start()
            except TransportError as exc:
                # Refused rather than skipped. An adapter quietly left out produces a display
                # identical to a working sensor over an empty field (FM-010), and the whole
                # point of starting in live mode is that what is on screen is a measurement.
                for started, _ in adapters:
                    started.stop()
                raise StartupRefused(
                    f"sensor {adapter.sensor_id!r} could not be opened: {exc}\n"
                    "  Check the cabling and the port, then prove the link with:\n"
                    f"    python -m scripts.radar_probe --port {port}\n"
                    "  See docs/hardware_radar_bench.md."
                ) from exc
            _LOG.info("Sensor %s started on %s", adapter.sensor_id, port)
            self.attach(adapter)

        return len(adapters)

    def attach(self, adapter: SensorAdapter) -> None:
        """Add an already-constructed, already-started source.

        The seam the tests use to drive the whole pipeline from a fake transport, and the one
        `attach_live_sensors` builds on. Deliberately typed against `SensorAdapter` and not
        against either concrete adapter: the moment this signature names a kind of sensor, the
        pipeline knows something about its sources that it is not supposed to know.
        """
        self._adapters.append(adapter)

    @property
    def has_sources(self) -> bool:
        """Whether anything is feeding the pipeline. Nothing attached means nothing to step."""
        return bool(self._adapters)

    def real_adapter_count(self) -> int:
        """Attached adapters that are reading real hardware rather than a simulation.

        Counted from the adapters themselves rather than from the configured run mode, because
        the two can disagree — a deployment declaring LIVE with an unplugged sensor is exactly
        the case `GET /system/mode` exists to expose.
        """
        return sum(1 for adapter in self._adapters if not adapter.sensor_type.is_simulated)

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


def build_application(
    site: str = DEFAULT_SITE,
    scenario: str | None = None,
    data_dir: Path | None = None,
    tick_s: float = DEFAULT_TICK_S,
    authenticated: bool = True,
    live: bool = False,
    radar_port: str | None = None,
):
    """Assemble the pipeline and the API around it.

    `authenticated` selects the identity provider. True is the default and the only setting safe
    off loopback; False attributes everything to one local operator with no login at all
    (ADR 0008, FM-001) and exists for a single-user machine. `guard_bind` refuses to combine it
    with a network interface.

    `live` opens the sensors the site declares as fitted instead of driving simulated ones from
    a scenario. The two are refused together: they would put synthetic and real detections on
    one display, and the mode indicator can then only be a half-truth. A genuine hybrid — real
    sensors alongside a synthetic target for training — is a HYBRID-mode question with its own
    answers about how the interface marks each track, and it is not answered by letting the
    flags combine.
    """
    if live and scenario:
        raise StartupRefused(
            "--live and --scenario cannot be combined: real and synthetic detections would "
            "share one display with nothing on screen distinguishing them. Run one or the "
            "other."
        )

    clock = SystemClock()
    geometry = load_site_geometry(site)
    resolved = data_dir or ConfigPaths.default().repo_root / "data"

    pipeline = Pipeline(clock, geometry, resolved)
    if scenario:
        pipeline.attach_scenario(scenario)
    if live:
        started = pipeline.attach_live_sensors(radar_port)
        _LOG.info("Live mode: %d fitted sensor(s) reading at site %s", started, site)

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
        run_mode=RunMode.LIVE if live else RunMode.SIMULATION,
        real_adapters=pipeline.real_adapter_count,
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
    """
    existing_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope_app):
        # Enter the API's own lifespan first: it catches the projection up and starts the
        # broadcaster, and the pipeline should not be appending events before the projection
        # has absorbed whatever a previous run left behind.
        async with existing_lifespan(scope_app):
            handle: asyncio.Task | None = None

            # Whatever is attached — a scenario's simulated sensors or fitted hardware — is
            # stepped by the same loop. Nothing below this line knows which it is, which is
            # the governing principle holding at the one place it would be easiest to break.
            if pipeline.has_sources:

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
        "--live",
        action="store_true",
        help=(
            "read the sensors this site declares as fitted (config/hardware/) instead of "
            "driving simulated ones. Refuses to combine with --scenario."
        ),
    )
    parser.add_argument(
        "--radar-port",
        default=None,
        help=(
            "serial port to read the fitted radar on, overriding the one in its hardware "
            "document. For a bench run where the port moves between machines."
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
            live=arguments.live,
            radar_port=arguments.radar_port,
        )
    except StartupRefused as refusal:
        # A sensor that will not open, or a flag combination that would misrepresent what is
        # on screen, stops the process here rather than surfacing as a quiet display later.
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
