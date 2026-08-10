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
from pathlib import Path

import uvicorn

from backend.alerting.policy import load_alert_policy
from backend.alerting.service import AlertService
from backend.api.app import create_app
from backend.api.identity import LocalOperatorIdentityProvider
from backend.api.services import ApiServices
from backend.api.static import mount_frontend
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
        self.alerting = AlertService(load_alert_policy(), self.events)

        # Alerts outlive the process that raised them. Rebuilding from the log before the
        # first cycle is what makes that true — without it a restart discharges every open
        # alert, and an operator cannot tell an empty feed from a quiet sky.
        self.alerting.rebuild()

        self._adapters: list[SimulatedSensorAdapter] = []

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


def build_application(
    site: str = DEFAULT_SITE,
    scenario: str | None = None,
    data_dir: Path | None = None,
    tick_s: float = DEFAULT_TICK_S,
):
    """Assemble the pipeline and the API around it."""
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
        identity=LocalOperatorIdentityProvider(clock),
        alerts=pipeline.alerting,
        classification_running=True,
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
    arguments = parser.parse_args()

    app = build_application(
        site=arguments.site,
        scenario=arguments.scenario,
        data_dir=arguments.data_dir,
        tick_s=arguments.tick,
    )
    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="info")


if __name__ == "__main__":
    main()
