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
from backend.fusion.policy import load_fusion_policy
from backend.fusion.reconcile import DetectionReconciler, to_incoming
from backend.ingestion.pipeline import IngestionPipeline
from backend.risk.engine import build_risk_engine, load_risk_policy
from backend.risk.service import RiskService
from backend.system_health.cycles import CycleHealth
from backend.tracking.track_manager.manager import TrackManager
from backend.zones.geometry import SiteGeometry, load_site_geometry
from scenarios.library import load_all
from scenarios.world import ScenarioWorld
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
        self.fusion = DetectionReconciler(load_fusion_policy())
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

        self.cycles = CycleHealth()
        """How this process's cycles are going, read by the health endpoint (FM-032).

        Owned here rather than inside `step`, because the thing worth reporting is whether the
        cycle *returned* — which only the caller driving it can know.
        """

        # Alerts outlive the process that raised them. Rebuilding from the log before the
        # first cycle is what makes that true — without it a restart discharges every open
        # alert, and an operator cannot tell an empty feed from a quiet sky.
        self.alerting.rebuild()

        self._recover_tracks_from_previous_run()

        self._adapters: list[SimulatedSensorAdapter] = []

        # The site and its sensors are configuration the API reads back, so they are persisted
        # once at startup rather than being loaded independently by every request.
        self.repositories.sites.upsert(geometry.site)
        self._persist_sensors()

    def _recover_tracks_from_previous_run(self) -> None:
        """Close out tracks the previous process left open, and resume identifiers above them.

        FM-030. The projection is caught up first: it is the record of what the last process
        got as far as writing, and reconciling against a stale view would leave exactly the
        tracks this exists to close. The tracker is then told every identifier ever issued (so
        it cannot mint a colliding one) and which tracks are still live (so it can retire
        them), and the projection is advanced again to apply the retirements it emitted.

        Ordering against `alerting.rebuild()` above matters and is deliberate: alerts are
        restored first, then their tracks are retired, so the first cycle clears each restored
        alert with `track_lost` rather than leaving it pointing at a track nothing maintains.
        An alert that was acknowledged before the restart therefore re-raises unacknowledged if
        the object is still there. That is the intended reading — an acknowledgement attests
        that a human saw a particular track, and after a discontinuity in which the system lost
        every track, asking them to look again is the safe direction to be wrong in.
        """
        self.projection.catch_up()

        known = [projected.track for projected in self.repositories.tracks.list_all()]
        if not known:
            return

        closed = self.tracker.resume_after_restart(
            known_track_ids=[track.track_id for track in known],
            live_track_ids=[track.track_id for track in known if track.state.is_active],
        )
        self.projection.catch_up()

        if closed:
            _LOG.warning(
                "Closed %d track(s) left open by a previous run. Track continuity does not "
                "survive a restart (FM-030); present objects are re-acquired under new "
                "identifiers.",
                closed,
            )

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

        # Several sensors watching one object each produce a detection of it, and the tracker
        # would start a track per detection (FM-023). Reconciling here — before tracking, on
        # detections rather than on tracks — is what makes one object one track. See ADR 0018
        # for why this cannot live in association.
        reconciled = self.fusion.reconcile(ingested.for_tracking)
        tracks = self.tracker.process([to_incoming(f) for f in reconciled.detections])

        classified = self.classification.classify_cycle(
            tracks,
            # Fused measurements carry their parts, so a track observed by three sensors is
            # classified on three hints rather than on whichever one survived fusion (FM-024).
            CycleEvidence.from_fused_assignments(
                self.tracker.last_assignments,
                {f.detection_id: f.contributing_detection_ids for f in reconciled.detections},
                ingested.hints,
                ingested.quality,
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


def guard_bind(
    host: str,
    *,
    authenticated: bool,
    behind_proxy: bool,
    user_count: int,
    published_loopback_only: bool = False,
) -> None:
    """Refuse to start in a configuration that would put an open console on a network.

    Three refusals, each for a state that is survivable on a laptop and not survivable on a
    site:

    1. **No authentication, off loopback.** The FM-001 state. Anyone who can route to the port
       gets the full operator interface and the complete event log.
    2. **No accounts, off loopback.** Authentication is configured but nobody can log in, and an
       unusable console is the kind of thing that gets "fixed" at 2am with --no-auth.
    3. **Off loopback with no explicit assertion about why that's safe.** Two different
       situations bind a non-loopback interface, and each gets its own flag rather than sharing
       one:

       - `--behind-proxy`: a real reverse proxy terminates TLS in front of this process. Also
         enables trusting `X-Forwarded-Proto` from that proxy, which is what marks the session
         cookie `Secure`.
       - `--published-loopback-only`: this process is a container. It has to bind `0.0.0.0`
         because container port publishing cannot reach a socket bound to `127.0.0.1` inside the
         network namespace — but the *published* port itself is restricted to loopback on the
         host (see `docker-compose.yml`), so the traffic this process actually serves never
         reaches a real network. Deliberately does **not** enable trusting `X-Forwarded-Proto`:
         nothing is proxying, so the cookie correctly stays non-`Secure`, exactly as it would on
         a literal loopback bind.

       Neither is inferred from a header or from the host string, because both are supplied by
       whoever configured the process, not by whoever is connecting to it.

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

    if not behind_proxy and not published_loopback_only:
        raise StartupRefused(
            f"Refusing to bind {host} without --behind-proxy or --published-loopback-only. "
            "Aetos does not terminate TLS, so binding a non-loopback interface needs an "
            "explicit assertion about why that's safe: either a reverse proxy handles TLS "
            "(--behind-proxy), or this is a container whose published port is restricted to "
            "loopback on the host (--published-loopback-only). See docs/deployment.md."
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
        fusion_running=pipeline.fusion.policy.enabled,
        situation=pipeline.risk,
        cycles=pipeline.cycles,
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
                    while True:
                        try:
                            # The tracker and the store are synchronous and are the only
                            # writers, so this runs on the loop thread rather than in an
                            # executor. That keeps the single-writer discipline (ADR 0006)
                            # true by construction rather than by agreement between two
                            # threads.
                            pipeline.step()
                            pipeline.cycles.record_success(pipeline.clock.now())
                        except Exception as exc:
                            # An unhandled exception here used to kill this task outright.
                            # The server went on serving, the interface went on reporting a
                            # live stream, and nothing was being tracked — a confident
                            # display over dead data, which is the failure this whole system
                            # is built to avoid. It is now logged loudly and the loop
                            # continues, so a transient fault costs one cycle rather than
                            # the run.
                            # Recorded as well as logged. A log line is only loud to
                            # someone already reading logs; the health endpoint is what a
                            # monitoring probe and the interface actually consult, and until
                            # this was written there they both reported a frozen pipeline as
                            # healthy (FM-032).
                            pipeline.cycles.record_failure(pipeline.clock.now(), exc)
                            _LOG.exception(
                                "Pipeline cycle failed (%d consecutive). The operator "
                                "picture is not advancing.",
                                pipeline.cycles.consecutive_failures,
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
            "bind anything but loopback (unless --published-loopback-only applies instead). "
            "Also trusts X-Forwarded-* from that proxy."
        ),
    )
    parser.add_argument(
        "--published-loopback-only",
        action="store_true",
        help=(
            "assert that this process runs in a container: it must bind 0.0.0.0 for the "
            "container's port publishing to work, but the published port itself is restricted "
            "to loopback on the host (see docker-compose.yml). Required to bind anything but "
            "loopback in place of --behind-proxy. Unlike --behind-proxy, does not trust "
            "X-Forwarded-* — nothing is actually proxying."
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

    if arguments.behind_proxy and arguments.published_loopback_only:
        parser.error(
            "--behind-proxy and --published-loopback-only assert different things and cannot "
            "both be true: pick the one that actually describes this deployment."
        )

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
            published_loopback_only=arguments.published_loopback_only,
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
