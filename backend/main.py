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
from datetime import datetime
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
from backend.fusion.policy import load_fusion_policy
from backend.fusion.service import FusionService
from backend.ingestion.pipeline import IngestionPipeline
from backend.risk.engine import build_risk_engine, load_risk_policy
from backend.risk.service import RiskService
from backend.system_health.policy import load_health_policy
from backend.system_health.watchdog import SensorWatchdog
from backend.tracking.track_manager.manager import TrackManager
from backend.zones.geometry import SiteGeometry, load_site_geometry
from scenarios.library import load_all
from scenarios.schema import AvailabilityState, Scenario
from scenarios.world import ScenarioWorld
from sensors.adapters.simulated.adapter import SimulatedSensorAdapter
from sensors.adapters.simulated.degradation import NOMINAL, SensorDegradation
from sensors.adapters.simulated.registry import load_sensor_registry
from sensors.schemas.detection import Detection

_LOG = logging.getLogger("aetos.pipeline")

DEFAULT_SITE = "fictional_test_range"
DEFAULT_TICK_S = 0.5

SESSION_PURGE_INTERVAL_S = 900.0
"""How often expired sessions are swept from the database.

Sessions are the one kind of row that is genuinely deleted, but nothing deleted them: `validate`
revokes a session only when its holder presents the token, so a session whose owner never
returns — the ordinary case for a closed browser — outlived its own expiry indefinitely. The
rows were unusable (expiry is enforced on read), so this is table growth rather than an
authentication hole, but on a console left running for months it is unbounded growth.

Fifteen minutes rather than every cycle: the sweep is a `DELETE` over an indexed column and the
shortest expiry in play is measured in hours, so running it at the pipeline's 2 Hz would be
pure overhead.
"""


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
        # Between tracking and classification, per docs/architecture.md. Everything downstream
        # of here sees objects rather than tracks, which is the whole of FM-023.
        self.fusion = FusionService(load_fusion_policy(), self.events, sink=self.tracker)

        # Reads the adapters' own health reports and decides what they mean. Given the sensor
        # registry's update rates below, so silence is judged against what each sensor was
        # configured to do rather than against one global number (FM-010).
        self.watchdog = SensorWatchdog(load_health_policy(), clock, self.events)

        self.alerting = AlertService(load_alert_policy(), self.events)

        # Alerts outlive the process that raised them. Rebuilding from the log before the
        # first cycle is what makes that true — without it a restart discharges every open
        # alert, and an operator cannot tell an empty feed from a quiet sky.
        self.alerting.rebuild()

        self._adapters: list[SimulatedSensorAdapter] = []
        self._scenario: Scenario | None = None
        self._scenario_started_at: datetime | None = None

        self._last_session_purge_monotonic: float | None = None
        """None until the first sweep, so one runs on the first cycle — a restart is exactly
        when a backlog of sessions abandoned by the previous run is waiting."""

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
        self._scenario = scenario
        self._scenario_started_at = self.clock.now()
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
            # The watchdog does not read the sensor registry itself — it would then only work
            # for sensors that have registry entries, which real hardware may not. Whatever
            # assembles the adapters knows the rate, so it tells the watchdog.
            self.watchdog.set_expected_interval(
                adapter.sensor_id, adapter.model.update_period_s
            )

        self._check_availability_targets(scenario)
        self._apply_availability()

    def _check_availability_targets(self, scenario: Scenario) -> None:
        """Refuse a scenario that scripts a sensor this site does not have.

        Silently doing nothing is the failure this exists to prevent, and it is not
        hypothetical: `e_sensor_degradation` scripted a radar going offline at 200 s against a
        sensor id no site defines, so the one scenario written to exercise sensor failure
        exercised nothing, and did so without a word. A scenario that names a sensor which is
        not there is a configuration mistake, and a loud one is cheaper than a quiet one.
        """
        # Every placement the site declares, not only the enabled ones: a window naming a
        # sensor that exists but is switched off is inert by design and not a mistake, while a
        # window naming a sensor that does not exist anywhere is always one.
        known = {placement.sensor_id for placement in self.geometry.site.sensors}
        unknown = sorted({w.sensor_id for w in scenario.sensor_availability} - known)
        if unknown:
            raise ValueError(
                f"scenario {scenario.scenario_id} scripts availability for sensors this site "
                f"does not have: {', '.join(unknown)}. Site sensors are: "
                f"{', '.join(sorted(known))}."
            )

    def _apply_availability(self) -> None:
        """Put each adapter into the performance its scenario scripts for right now.

        Recomputed from the windows every cycle rather than applied on transitions, so a window
        that opens and closes between two cycles cannot be missed and the adapter's state is
        always a pure function of elapsed scenario time — which is what keeps a replay of the
        same scenario identical to the original run (ADR 0008).
        """
        if self._scenario is None or self._scenario_started_at is None:
            return

        elapsed = (self.clock.now() - self._scenario_started_at).total_seconds()

        for adapter in self._adapters:
            degradation = NOMINAL
            for window in self._scenario.sensor_availability:
                if window.sensor_id != adapter.sensor_id:
                    continue
                if elapsed < window.from_s:
                    continue
                if window.to_s is not None and elapsed >= window.to_s:
                    continue
                # Later windows win, so a scenario can script recovery by declaring an online
                # window after an offline one.
                degradation = SensorDegradation(
                    offline=window.state is AvailabilityState.OFFLINE,
                    detection_probability_scale=window.detection_probability_scale,
                    noise_scale=window.noise_scale,
                    false_alarm_rate_scale=window.false_alarm_rate_scale,
                    reason=window.reason,
                )
            adapter.apply_degradation(degradation)

    def step(self) -> int:
        """Run one cycle. Returns how many detections were accepted.

        The order is the pipeline in `docs/architecture.md`: sensors, ingestion, tracking,
        classification, risk, alerting, projection. Each stage sees only what the one before
        it produced — risk runs after classification because identity is one of its inputs,
        and a track assessed before it was classified would be scored on geometry alone for
        one cycle longer than necessary.
        """
        # Before polling: a scripted outage has to be in force for the cycle it covers, not the
        # one after it.
        self._apply_availability()

        detections: list[Detection] = []
        for adapter in self._adapters:
            detections.extend(adapter.get_detections())

        # Validates, deduplicates, records DETECTION_RECEIVED, and reduces to the tracker's
        # input. The classification hints come back separately — they are evidence about what
        # a track is, and must not reach the filter deciding where it is (ADR 0002).
        ingested = self.ingestion.ingest(detections)

        tracks = self.tracker.process(ingested.for_tracking)

        # Reconcile multi-sensor observations before anything downstream counts them. One drone
        # seen by three sensors is three tracks and one object; classification, risk and
        # alerting all see the object, so it is classified once, scored once and alerted on
        # once (FM-023). The supporting tracks stay tracked and stay in the log.
        objects = self.fusion.fuse_cycle(tracks)

        classified = self.classification.classify_cycle(
            objects,
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
        # Polled every cycle rather than on a timer: sensor health is cheap to read, and a
        # watchdog that sampled would report a gap it happened to look through.
        self.watchdog.poll([adapter.health() for adapter in self._adapters])

        self.projection.catch_up()
        self._purge_sessions_if_due()
        return ingested.accepted

    def _purge_sessions_if_due(self) -> None:
        """Sweep expired sessions on a slow timer. See `SESSION_PURGE_INTERVAL_S`.

        Uses `monotonic()` rather than `now()` because this is interval logic: wall time can
        step backwards under clock correction, and a sweep that then stopped running until wall
        time caught up would be a leak visible only after an NTP adjustment (ADR 0003).
        """
        elapsed = self.clock.monotonic()
        if (
            self._last_session_purge_monotonic is not None
            and elapsed - self._last_session_purge_monotonic < SESSION_PURGE_INTERVAL_S
        ):
            return

        self._last_session_purge_monotonic = elapsed
        purged = self.auth.purge_expired_sessions()
        if purged:
            _LOG.info("purged %d expired session(s)", purged)

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
):
    """Assemble the pipeline and the API around it.

    `authenticated` selects the identity provider. True is the default and the only setting safe
    off loopback; False attributes everything to one local operator with no login at all
    (ADR 0008, FM-001) and exists for a single-user machine. `guard_bind` refuses to combine it
    with a network interface.
    """
    clock = SystemClock()
    geometry = load_site_geometry(site)
    resolved = data_dir or ConfigPaths.default().repo_root / "data"

    pipeline = Pipeline(clock, geometry, resolved)
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
        fusion=pipeline.fusion,
        watchdog=pipeline.watchdog,
        situation=pipeline.risk,
        run_mode=RunMode.SIMULATION,
    )

    app = create_app(services)
    _attach_pipeline_task(app, pipeline, scenario, tick_s)

    # After the routers, so the interface's catch-all can only ever match paths the API did
    # not claim.
    mount_frontend(app)
    return app


def _attach_pipeline_task(app, pipeline: Pipeline, scenario: str | None, tick_s: float) -> None:
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

            if scenario:

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
    app = build_application(
        site=arguments.site,
        scenario=arguments.scenario,
        data_dir=arguments.data_dir,
        tick_s=arguments.tick,
        authenticated=authenticated,
    )

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
