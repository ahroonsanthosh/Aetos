"""The browser camera, end to end: start it, send frames, see tracks and the badge change."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.auth import admin
from backend.common.config import ConfigPaths, RunMode
from backend.main import build_application

OPERATOR = ("camera_operator", "a-long-enough-password")
VIEWER = ("camera_viewer", "another-long-password")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo_root = tmp_path / "install"
    (repo_root / "data").mkdir(parents=True)
    real = ConfigPaths.default()
    monkeypatch.setattr(
        ConfigPaths,
        "default",
        classmethod(lambda cls: cls(repo_root=repo_root, config_root=real.config_root)),
    )
    for (username, password), role in ((OPERATOR, "operator"), (VIEWER, "viewer")):
        monkeypatch.setattr(admin, "_read_password", lambda *a, _p=password, **k: _p)
        assert admin.main(["add", "--username", username, "--role", role]) == 0

    app = build_application(
        site="fictional_test_range", data_dir=repo_root / "run", mode=RunMode.SIMULATION
    )
    with TestClient(app) as test_client:
        yield test_client


def sign_in(client: TestClient, who: tuple[str, str]) -> None:
    response = client.post("/api/v1/auth/login", json={"username": who[0], "password": who[1]})
    assert response.status_code == 200


FRAME = {
    "width": 160,
    "height": 90,
    "regions": [{"x": 78, "y": 43, "width": 4, "height": 4, "strength": 1.0}],
}


def test_an_idle_server_reports_no_camera(client: TestClient) -> None:
    sign_in(client, VIEWER)
    body = client.get("/api/v1/camera").json()
    assert body["available"] is True
    assert body["active"] is False
    assert body["state"] == "not_started"
    assert len(body["site_reference"]) == 3


def test_a_viewer_may_look_but_not_feed(client: TestClient) -> None:
    sign_in(client, VIEWER)
    assert client.put("/api/v1/camera/session", json={"boresight_deg": 0}).status_code == 403
    assert client.post("/api/v1/camera/frames", json=FRAME).status_code == 403


def test_frames_without_a_session_are_refused(client: TestClient) -> None:
    sign_in(client, OPERATOR)
    assert client.post("/api/v1/camera/frames", json=FRAME).status_code == 409


def test_a_region_outside_the_frame_is_refused(client: TestClient) -> None:
    sign_in(client, OPERATOR)
    client.put("/api/v1/camera/session", json={"boresight_deg": 0})
    bad = {**FRAME, "regions": [{"x": 150, "y": 0, "width": 20, "height": 4}]}
    assert client.post("/api/v1/camera/frames", json=bad).status_code == 422


def test_a_running_camera_feeds_the_pipeline_and_flips_the_badge(client: TestClient) -> None:
    sign_in(client, OPERATOR)
    assert client.get("/api/v1/system/mode").json()["simulated"] is True

    started = client.put("/api/v1/camera/session", json={"boresight_deg": 45, "tilt_deg": 15})
    assert started.status_code == 200
    assert started.json()["active"] is True
    assert started.json()["mount"]["boresight_deg"] == 45

    mode = client.get("/api/v1/system/mode").json()
    assert mode["simulated"] is False
    assert mode["real_adapters_connected"] == 1

    assert client.post("/api/v1/camera/frames", json=FRAME).json() == {"regions": 1}

    stopped = client.delete("/api/v1/camera/session").json()
    assert stopped["active"] is False
    assert client.get("/api/v1/system/mode").json()["simulated"] is True


def test_camera_frames_become_detections_and_a_track(tmp_path: Path) -> None:
    """Through ingestion and the tracker, like any sensor: logged, then tracked."""
    from datetime import UTC, datetime

    from backend.common.clock import SimClock
    from backend.events.types import EventType
    from backend.main import Pipeline
    from backend.zones.geometry import load_site_geometry
    from sensors.adapters.camera import CameraFrame, CameraMount, MotionRegion

    clock = SimClock(datetime(2026, 1, 1, tzinfo=UTC))
    pipeline = Pipeline(clock, load_site_geometry("fictional_test_range"), tmp_path)
    try:
        lat, lon, alt = pipeline.camera.site_reference()
        pipeline.camera.start(
            CameraMount(latitude=lat, longitude=lon, altitude_m=alt + 2, boresight_deg=0.0)
        )
        for step in range(12):
            region = MotionRegion(x=76 + step, y=40, width=4, height=4, strength=1.0)
            pipeline.camera.submit(CameraFrame(width=160, height=90, regions=(region,)))
            pipeline.step()
            clock.advance(0.5)

        events = list(pipeline.events.read_from(1))
        received = [e for e in events if e.event_type is EventType.DETECTION_RECEIVED]
        assert len(received) == 12
        assert any(e.event_type is EventType.TRACK_CONFIRMED for e in events)
    finally:
        pipeline.close()


def test_an_abandoned_camera_is_detached(tmp_path: Path) -> None:
    """A page closed without saying so must not hold the badge at LIVE forever."""
    from datetime import UTC, datetime

    from backend.common.clock import SimClock
    from backend.main import CAMERA_EXPIRY_S, Pipeline
    from backend.zones.geometry import load_site_geometry
    from sensors.adapters.camera import CameraMount

    clock = SimClock(datetime(2026, 1, 1, tzinfo=UTC))
    pipeline = Pipeline(clock, load_site_geometry("fictional_test_range"), tmp_path)
    try:
        lat, lon, alt = pipeline.camera.site_reference()
        pipeline.camera.start(
            CameraMount(latitude=lat, longitude=lon, altitude_m=alt, boresight_deg=0.0)
        )
        assert pipeline.live_adapter_count == 1
        clock.advance(CAMERA_EXPIRY_S + 1)
        pipeline.step()
        assert pipeline.live_adapter_count == 0
        assert pipeline.camera.status().active is False
    finally:
        pipeline.close()
