"""The browser camera: start it, stop it, and hand it the regions that moved.

The console's Camera page opens the operator's own camera, finds motion in the browser and
posts the moving regions here, a few times a second. No video is uploaded. The regions go to a
`CameraAdapter`, which turns them into bearing-only detections that enter the pipeline through
ingestion like every other sensor's.

Reading the camera's state needs `viewer`. Starting it, stopping it and sending frames need
`operator`, because each of those puts detections into the operational picture.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.api.deps import RequireOperator, ServicesDep, require_role
from backend.api.services import ApiServices, CameraSource, CameraStatus
from backend.auth.models import Role
from sensors.adapters.camera import MAX_REGIONS_PER_FRAME, CameraFrame, CameraMount, MotionRegion
from sensors.adapters.interface import SensorAdapterError

router = APIRouter(dependencies=[Depends(require_role(Role.VIEWER))])


class MountView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float
    longitude: float
    altitude_m: float
    boresight_deg: float
    tilt_deg: float
    horizontal_fov_deg: float
    vertical_fov_deg: float
    assumed_range_m: float
    range_sigma_m: float


class CameraStatusView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool
    """False when this deployment has no camera bridge at all."""
    active: bool
    sensor_id: str | None = None
    state: str
    reason: str
    frames_received: int = 0
    detections_emitted: int = 0
    seconds_since_frame: float | None = None
    mount: MountView | None = None
    site_reference: tuple[float, float, float] | None = None
    """Latitude, longitude and altitude the camera defaults to when no position is given."""

    @classmethod
    def of(cls, camera: CameraSource, current: CameraStatus) -> CameraStatusView:
        return cls(
            available=True,
            active=current.active,
            sensor_id=current.sensor_id,
            state=current.state.value,
            reason=current.reason,
            frames_received=current.frames_received,
            detections_emitted=current.detections_emitted,
            seconds_since_frame=current.seconds_since_frame,
            mount=MountView(**current.mount.model_dump()) if current.mount else None,
            site_reference=camera.site_reference(),
        )


class CameraSessionRequest(BaseModel):
    """Where the camera is and which way it points. Position defaults to the site reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    height_m: float = Field(default=1.5, ge=0.0, le=500.0)
    """Lens height above the site reference altitude."""
    boresight_deg: float = Field(ge=0.0, lt=360.0)
    tilt_deg: float = 0.0
    horizontal_fov_deg: float = 66.0
    vertical_fov_deg: float = 40.0
    assumed_range_m: float = 300.0
    range_sigma_m: float = 250.0


class RegionIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float = Field(ge=0.0)
    y: float = Field(ge=0.0)
    width: float = Field(gt=0.0)
    height: float = Field(gt=0.0)
    strength: float = Field(default=0.5, ge=0.0, le=1.0)


class FrameIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    width: int = Field(ge=16, le=8192)
    height: int = Field(ge=16, le=8192)
    regions: list[RegionIn] = Field(default_factory=list, max_length=MAX_REGIONS_PER_FRAME)


class FrameAccepted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regions: int


def _camera(services: ApiServices) -> CameraSource:
    if services.camera is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="This deployment has no camera bridge.",
        )
    return services.camera


@router.get("/camera", response_model=CameraStatusView, tags=["camera"])
async def get_camera(services: ServicesDep) -> CameraStatusView:
    if services.camera is None:
        return CameraStatusView(
            available=False,
            active=False,
            state="not_started",
            reason="This deployment has no camera bridge.",
        )
    return CameraStatusView.of(services.camera, services.camera.status())


@router.put("/camera/session", response_model=CameraStatusView, tags=["camera"])
async def start_camera(
    services: ServicesDep, _operator: RequireOperator, body: CameraSessionRequest
) -> CameraStatusView:
    """Start the camera feeding the picture, or re-aim one already running."""
    camera = _camera(services)
    ref_lat, ref_lon, ref_alt = camera.site_reference()
    try:
        mount = CameraMount(
            latitude=ref_lat if body.latitude is None else body.latitude,
            longitude=ref_lon if body.longitude is None else body.longitude,
            altitude_m=ref_alt + body.height_m,
            boresight_deg=body.boresight_deg,
            tilt_deg=body.tilt_deg,
            horizontal_fov_deg=body.horizontal_fov_deg,
            vertical_fov_deg=body.vertical_fov_deg,
            assumed_range_m=body.assumed_range_m,
            range_sigma_m=body.range_sigma_m,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors(include_url=False)
        ) from exc
    return CameraStatusView.of(camera, camera.start(mount))


@router.delete("/camera/session", response_model=CameraStatusView, tags=["camera"])
async def stop_camera(services: ServicesDep, _operator: RequireOperator) -> CameraStatusView:
    camera = _camera(services)
    camera.stop()
    return CameraStatusView.of(camera, camera.status())


@router.post(
    "/camera/frames",
    response_model=FrameAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["camera"],
)
async def submit_frame(
    services: ServicesDep, _operator: RequireOperator, frame: FrameIn
) -> FrameAccepted:
    """One analysed frame: its size and the regions in it that moved."""
    camera = _camera(services)
    for region in frame.regions:
        if region.x + region.width > frame.width or region.y + region.height > frame.height:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A region extends outside the frame.",
            )
    try:
        count = camera.submit(
            CameraFrame(
                width=frame.width,
                height=frame.height,
                regions=tuple(MotionRegion(**r.model_dump()) for r in frame.regions),
            )
        )
    except SensorAdapterError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return FrameAccepted(regions=count)
