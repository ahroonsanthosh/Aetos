"""Runnable entry point: wires the pipeline together and serves the API.

Everything below this line existed already as modules that tests constructed individually.
This is the one place they are assembled into a system that runs, which is what makes the
repository usable rather than merely correct.

    python -m backend.main                          serve the API against an empty log
    python -m backend.main --scenario a_benign_transit   serve, and drive that scenario
    python -m backend.main --live                   serve, driven by the physical sensors

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
from collections.abc import Sequence
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
from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.registry import load_sensor_registry
from sensors.config.loader import SensorConfig, SensorConfigError, load_sensor_config
from sensors.live import LiveSensor, build_live_sensors
from sensors.schemas.detection import Detection

_LOG = logging.getLogger("aetos.pipeline")


class RunModeConflict(ValueError):
    """A run was asked to be both simulated and live.

    Its own type rather than a bare ValueError: `main()` turns this into a clean message, and
    catching ValueError there would also swallow every pydantic ValidationError raised while
    assembling the pipeline — reporting a malformed site file as if it were a mode conflict.
    """

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

        # Typed to the interface, not to the simulated adapter. `step()` polls this list and
        # must stay unable to tell what kind of source is in it; adding a physical sensor is
        # then an addition to whatever builds the list and no change to the loop that reads it.
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

    def attach_live_sensors(self, config: SensorConfig | None = None) -> list[str]:
        """Build the physical sensors from configuration and start them. Returns their ids.

        The counterpart to `attach_scenario`, and deliberately the same shape: both populate
        `self._adapters` and start what they built, and `step()` cannot tell which one ran.
        That is the property the whole architecture is for — a live run and a simulated run
        differ in this one call and nowhere else.

        Which sensors exist is `sensors.live`'s business, not this module's. Nothing here
        names a camera or a radar, so replacing one is a change to that file alone.

        Adapters that fail to come up are kept rather than dropped. A camera whose stream is
        down reports OFFLINE through `health()` and the watchdog says so; silently omitting it
        would leave the operator interface showing a sensor list that does not mention the
        sensor that is broken.
        """
        sensors = build_live_sensors(
            clock=self.clock,
            # The backend's frame, from the site file — not the bench script's fallback origin.
            frame=self.geometry.frame,
            config=config if config is not None else load_sensor_config(),
        )

        self.start_live_sensors(sensors)
        return [sensor.sensor_id for sensor in sensors]

    def start_live_sensors(self, sensors: list[LiveSensor]) -> None:
        """Adopt a built sensor set: persist it, then start each sensor under its own guard.

        Split from `attach_live_sensors` so that a caller holding sensors it built itself —
        a test substituting a scripted frame source for a handset — goes through exactly this
        path rather than reimplementing it. A test that starts adapters its own way stops
        testing how they are actually started, which is how a swap passes its tests and then
        fails on the bench.
        """
        self._replace_adapters([sensor.adapter for sensor in sensors])
        self._persist_live_sensors(sensors)

        for sensor in sensors:
            try:
                sensor.adapter.start()
                health = sensor.adapter.health()
            except Exception:
                # One unimplemented or broken adapter must not prevent the others from
                # observing. It is logged at exception level and left in the set, where its
                # health record — or its absence — is the honest report. Dropping it would
                # remove the sensor from the operator's list precisely when it is the one
                # thing wrong.
                _LOG.exception(
                    "Live sensor %s failed to start; it will report no detections.",
                    sensor.sensor_id,
                )
                continue

            _LOG.info(
                "Live sensor %s (%s) started: %s — %s",
                sensor.sensor_id,
                sensor.adapter.sensor_type.value,
                health.state.value,
                health.reason,
            )

    @property
    def adapters(self) -> Sequence[SensorAdapter]:
        """The adapters currently feeding the pipeline. Read by the API's mode banner."""
        return tuple(self._adapters)

    def _replace_adapters(self, adapters: list[SensorAdapter]) -> None:
        """Swap the adapter set, stopping whatever was running first.

        Reassigning the list without stopping the old adapters strands whatever they hold — a
        camera's reader thread and its open socket outlive the adapter nobody can reach any
        more, and the stream stays consumed by a source that no longer feeds the pipeline.
        Attaching twice is not something the CLI does today, but the API and the tests both
        construct pipelines repeatedly, and a leak that only shows under repetition is one
        found late.
        """
        for adapter in self._adapters:
            adapter.stop()
        self._adapters = adapters

    def _persist_live_sensors(self, sensors: list[LiveSensor]) -> None:
        """Replace the site's persisted sensor list with the physical sensors that are running.

        Replacing rather than appending: in a live run the site file's simulated placements
        describe sensors that do not exist on this bench, and leaving them in the list would
        put four sensors on the operator map when two are real. A map showing sensors that are
        not there is worse than a sparse one.

        Positions are converted from each sensor's ENU offset through the site's own frame, so
        a live sensor lands where the site says it is rather than where a second, independent
        origin might put it.
        """
        site_id = self.geometry.site.site_id
        reference_altitude = self.geometry.site.reference.altitude_m

        records = []
        for sensor in sensors:
            geodetic = self.geometry.frame.to_geodetic(sensor.position_enu)
            records.append(
                SensorRecord(
                    sensor_id=sensor.sensor_id,
                    site_id=site_id,
                    registry_key=sensor.registry_key,
                    sensor_type=sensor.adapter.sensor_type,
                    latitude=geodetic.latitude_deg,
                    longitude=geodetic.longitude_deg,
                    height_m=geodetic.altitude_m - reference_altitude,
                    boresight_deg=sensor.boresight_deg,
                    enabled=True,
                )
            )

        self.repositories.sensors.replace_for_site(site_id, records)

    def attach_scenario(self, scenario_id: str) -> None:
        """Point the simulated sensors at a scenario's world."""
        scenario = load_all()[scenario_id]
        registry = load_sensor_registry()
        world = ScenarioWorld(scenario, self.clock.now())
        positions = self.geometry.site.sensor_positions_enu()

        self._replace_adapters([
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
        ])
        for adapter in self._adapters:
            adapter.start()

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
            try:
                detections.extend(adapter.get_detections())
            except Exception:
                # One sensor's fault must not blind the others. Without this, an adapter that
                # raises — a serial port yanked out, a driver not yet implemented, a decode
                # error — takes down the whole cycle, so a four-sensor site observes nothing
                # because one sensor broke. The failure is logged loudly rather than
                # swallowed, and that sensor contributes nothing this cycle while the rest of
                # the site goes on seeing (FM-010).
                _LOG.exception(
                    "Sensor %s failed during collection; it contributes nothing this cycle.",
                    adapter.sensor_id,
                )

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
            try:
                adapter.stop()
            except Exception:
                # Shutdown continues regardless. One sensor refusing to stop must not leave
                # the database and the event log unclosed behind it.
                _LOG.exception("Sensor %s failed to stop cleanly.", adapter.sensor_id)
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
):
    """Assemble the pipeline and the API around it.

    `authenticated` selects the identity provider. True is the default and the only setting safe
    off loopback; False attributes everything to one local operator with no login at all
    (ADR 0008, FM-001) and exists for a single-user machine. `guard_bind` refuses to combine it
    with a network interface.

    `live` drives the pipeline from the physical sensors in `sensor_config.yaml` instead of
    from a scenario. The two are mutually exclusive: a scenario supplies ground truth that
    simulated sensors degrade, and a physical sensor observes the actual world, so a run that
    tried to be both would be showing an operator two incompatible pictures at once. HYBRID is
    a named run mode for a future that reconciles them and is not this.
    """
    if live and scenario:
        raise RunModeConflict(
            "a run is driven by a scenario or by physical sensors, not both: "
            "a scenario is fabricated truth and a live sensor observes the real world, and "
            "showing an operator both at once would make neither trustworthy"
        )

    clock = SystemClock()
    geometry = load_site_geometry(site)
    resolved = data_dir or ConfigPaths.default().repo_root / "data"

    pipeline = Pipeline(clock, geometry, resolved)
    try:
        if live:
            pipeline.attach_live_sensors()
        elif scenario:
            pipeline.attach_scenario(scenario)
    except Exception:
        # Attaching opens streams and ports. If anything after that fails, the process may
        # still exit — `guard_bind` refuses some deployments outright — and a half-built
        # application must not leave a camera's reader thread and socket owned by nobody.
        pipeline.close()
        raise

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
        # Read live, so the interface's simulated-versus-real banner reflects what is actually
        # attached rather than what the launch flag claimed. A run started with --live but
        # carrying only fabricating sources must not be shown to an operator as real data.
        sensor_adapters=lambda: pipeline.adapters,
    )

    app = create_app(services)
    _attach_pipeline_task(app, pipeline, stepping=bool(scenario or live), tick_s=tick_s)

    # After the routers, so the interface's catch-all can only ever match paths the API did
    # not claim.
    mount_frontend(app)
    return app


def _attach_pipeline_task(app, pipeline: Pipeline, *, stepping: bool, tick_s: float) -> None:
    """Run the pipeline on the event loop for as long as the server is up.

    `stepping` rather than a scenario name: the loop is identical whether the detections come
    from a scenario or from physical sensors, and taking the name here would have made this
    function know which it was — the one thing the adapter boundary exists to prevent.

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

            if stepping:

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
        "--live",
        action="store_true",
        help=(
            "drive the pipeline from the physical sensors in sensors/config/sensor_config.yaml "
            "instead of from a scenario. Mutually exclusive with --scenario."
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
    try:
        app = build_application(
            site=arguments.site,
            scenario=arguments.scenario,
            data_dir=arguments.data_dir,
            tick_s=arguments.tick,
            authenticated=authenticated,
            live=arguments.live,
        )
    except SensorConfigError as bad_config:
        # A misconfigured sensor is an operator error with a fixable cause, so it gets the
        # cause rather than a traceback. Starting anyway with the sensor omitted would be
        # worse: the system would run, look healthy, and be watching nothing.
        raise SystemExit(f"\n  {bad_config}\n") from bad_config
    except RunModeConflict as conflict:
        raise SystemExit(f"\n  {conflict}\n") from conflict

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
