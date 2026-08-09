"""Wire models for the REST and WebSocket API.

These are separate types from the domain models on purpose. `Track`, `Site` and `Detection`
are shapes the tracking engine and the schema contract are free to change; the wire format is
a contract with a frontend that is versioned and deployed separately. Serialising the domain
models directly would couple every internal refactor to a client release, and the first time
that hurts is the first time it matters.

The separation also lets the boundary do work that the domain models should not:

- **ENU state is not exposed.** The tracker's authoritative position is metres in a
  site-local tangent plane (ADR 0004). Outside the backend, position is WGS-84 — that is what
  `docs/architecture.md` means by "WGS-84 at the schema and API edges". A client that received
  ENU metres would need the site reference point to interpret them, and would then own a
  coordinate transform the backend already owns.
- **Derived operator-facing judgements are computed here, once.** `displayable` is the
  backend's rule about what belongs on the map, sent as an answer rather than as inputs the
  frontend re-derives. The backend owns all authoritative state (spec §14), and that includes
  the rules over it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.database.projection import TRACK_SNAPSHOT_KEY
from backend.database.records import ProjectedTrack, ScenarioRecord, SensorRecord
from backend.events.types import Event, EventType
from backend.tracking.models import Track
from backend.zones.site import Site, Zone
from sensors.schemas.detection import Detection

API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"
"""Version in the path. The frontend and backend ship separately, so a breaking change must be
able to coexist with the version it breaks rather than requiring a simultaneous deployment."""

DETECTION_SNAPSHOT_KEY = "detection"
"""Payload key under which a `DETECTION_RECEIVED` event carries the detection it received.

Stated here because `backend/ingestion` does not exist yet; the same caveat applies as to the
track snapshot contract in `backend/database/projection.py`.
"""


class GeoPointView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float
    longitude: float


class PositionView(BaseModel):
    """WGS-84. Altitude is metres above the WGS-84 ellipsoid, as everywhere else (FM-004)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float
    longitude: float
    altitude_m: float


class KinematicsView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    speed_mps: float
    heading_deg: float
    vertical_rate_mps: float


class ClassificationView(BaseModel):
    """What the track is believed to be, and on what basis.

    Label and confidence are reported separately (spec §9), and both are *derived from*
    `distribution` rather than asserted beside it, so the headline cannot disagree with the
    detail behind it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    confidence: float
    """How much this conclusion should be believed — not the top label's probability. Folds
    distribution peakedness, evidence weight and track maturity, so a sharp answer built on
    one detection reads as uncertain (ADR 0012)."""

    distribution: dict[str, float] = Field(default_factory=dict)
    """Probability across the label set. Empty means nothing has been assessed, which is
    different from having been assessed as `unknown`."""

    reasons: list[str] = Field(default_factory=list)
    """One human-readable justification per contributing evidence source."""

    assessed: bool = False
    """Whether anything actually classified this track. Computed here so every client does
    not re-derive the same emptiness check and eventually derive it differently."""


class ThreatView(BaseModel):
    """Risk assessment. `reasons` travels with the score, never without it.

    A bare number is a black-box output an operator can neither act on nor challenge, so the
    API has no representation of a score without its reason codes (spec §10).

    `recommendation` is advice to a human and nothing else. No endpoint acts on it, and none
    ever will — Aetos ends at the recommendation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    risk_score: int
    risk_level: str
    reasons: list[str]
    recommendation: str


class TrackingView(BaseModel):
    """How well supported the track is, and on what."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    first_seen: datetime
    last_seen: datetime
    detection_count: int
    missed_updates: int
    coasting: bool
    track_confidence: float
    contributing_sensors: list[str]


class TrackView(BaseModel):
    """One track as the operator interface sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    track_id: str
    state: str

    displayable: bool
    """Whether this belongs on the operator's map. Computed by the backend, because a single
    unconfirmed return is exactly what clutter looks like and a client deciding for itself
    would eventually decide differently."""

    position: PositionView
    kinematics: KinematicsView
    classification: ClassificationView
    threat: ThreatView
    tracking: TrackingView
    updated_at: datetime

    last_applied_seq: int
    """The log position this view of the track was projected from. Lets a client tie a track
    to a point in the total order rather than to wall-clock time."""

    @classmethod
    def from_projection(cls, projected: ProjectedTrack) -> TrackView:
        return cls.from_track(projected.track, projected.last_applied_seq)

    @classmethod
    def from_track(cls, track: Track, last_applied_seq: int) -> TrackView:
        return cls(
            track_id=track.track_id,
            state=str(track.state),
            displayable=track.state.is_displayable,
            position=PositionView(
                latitude=track.position.latitude,
                longitude=track.position.longitude,
                altitude_m=track.position.altitude_m,
            ),
            kinematics=KinematicsView(
                speed_mps=track.kinematics.speed_mps,
                heading_deg=track.kinematics.heading_deg,
                vertical_rate_mps=track.kinematics.vertical_rate_mps,
            ),
            classification=ClassificationView(
                label=str(track.classification.label),
                confidence=track.classification.confidence,
                distribution={
                    str(label): probability
                    for label, probability in track.classification.distribution.items()
                },
                reasons=list(track.classification.reasons),
                assessed=track.classification.assessed,
            ),
            threat=ThreatView(
                risk_score=track.threat.risk_score,
                risk_level=str(track.threat.risk_level),
                reasons=list(track.threat.reasons),
                recommendation=str(track.threat.recommendation),
            ),
            tracking=TrackingView(
                first_seen=track.tracking.first_seen,
                last_seen=track.tracking.last_seen,
                detection_count=track.tracking.detection_count,
                missed_updates=track.tracking.missed_updates,
                coasting=track.tracking.is_coasting,
                track_confidence=track.tracking.track_confidence,
                contributing_sensors=list(track.tracking.contributing_sensors),
            ),
            updated_at=track.updated_at,
            last_applied_seq=last_applied_seq,
        )


class TrackListResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tracks: list[TrackView]
    count: int

    as_of_seq: int
    """The log position the projection was current to when this was answered.

    A client that opens the event stream at `as_of_seq + 1` sees neither a gap nor a
    duplicate across the handover from this snapshot to the live stream. Without it, the
    handover is a race the client cannot win.
    """


class TrackHistoryPointView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    event_type: EventType
    timestamp: datetime
    state: str
    position: PositionView
    speed_mps: float
    heading_deg: float


class TrackHistoryResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    track_id: str
    points: list[TrackHistoryPointView]
    count: int


class DetectionView(BaseModel):
    """A raw detection as recorded in the log.

    Debug and replay only. The operator interface displays tracks, never raw detections
    (spec §3) — see the endpoint documentation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    detection_id: str
    timestamp: datetime
    sensor_id: str
    sensor_type: str
    position: PositionView
    position_uncertainty_m: float
    confidence: float

    @classmethod
    def from_detection(cls, seq: int, detection: Detection) -> DetectionView:
        return cls(
            seq=seq,
            detection_id=detection.detection_id,
            timestamp=detection.timestamp,
            sensor_id=detection.sensor.sensor_id,
            sensor_type=str(detection.sensor.sensor_type),
            position=PositionView(
                latitude=detection.position.latitude,
                longitude=detection.position.longitude,
                altitude_m=detection.position.altitude_m,
            ),
            position_uncertainty_m=detection.measurement.position_uncertainty_m,
            confidence=detection.quality.confidence,
        )


class ScannedListResponse(BaseModel):
    """Base for responses that answer by scanning the log rather than reading a projection.

    `next_seq` is where a caller should resume. It exists because these endpoints bound the
    *scan*, not the result count: an answer of zero rows does not mean there are none, it may
    mean none were in the window scanned. Reporting the window honestly is the difference
    between an empty answer and a misleading one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_seq: int
    next_seq: int
    scanned: int


class DetectionListResponse(ScannedListResponse):
    detections: list[DetectionView]
    count: int


class AlertView(BaseModel):
    """An alert, derived from an `ALERT_CREATED` event.

    Loosely typed on purpose. The module that will produce alerts does not exist, so no alert
    model has been agreed; inventing fields here would publish a contract nothing has
    committed to. `detail` is the event payload as recorded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str
    seq: int
    created_at: datetime
    track_id: str | None
    source: str
    cleared: bool
    detail: dict[str, Any]


class AlertListResponse(ScannedListResponse):
    alerts: list[AlertView]
    count: int


class SensorView(BaseModel):
    """A sensor's placement and whether it is enabled.

    Note what is missing: operational health. Whether a sensor has gone silent belongs to
    `backend/system_health`, which does not exist. Reporting a health field here would report
    a status nothing computes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str
    site_id: str
    registry_key: str
    sensor_type: str | None
    position: GeoPointView
    height_m: float
    boresight_deg: float
    enabled: bool

    @classmethod
    def from_record(cls, record: SensorRecord) -> SensorView:
        return cls(
            sensor_id=record.sensor_id,
            site_id=record.site_id,
            registry_key=record.registry_key,
            sensor_type=str(record.sensor_type) if record.sensor_type else None,
            position=GeoPointView(latitude=record.latitude, longitude=record.longitude),
            height_m=record.height_m,
            boresight_deg=record.boresight_deg,
            enabled=record.enabled,
        )


class SensorListResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sensors: list[SensorView]
    count: int


class CircleGeometryView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: Literal["circle"] = "circle"
    centre: GeoPointView
    radius_m: float


class PolygonGeometryView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: Literal["polygon"] = "polygon"
    vertices: list[GeoPointView]
    """Ordered, and the ring closes implicitly — the first vertex is not repeated at the end.
    A client drawing the boundary closes it itself."""


class ZoneView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    zone_id: str
    name: str
    kind: str
    geometry: CircleGeometryView | PolygonGeometryView = Field(discriminator="shape")
    floor_m: float | None
    ceiling_m: float | None

    @classmethod
    def from_zone(cls, zone: Zone) -> ZoneView:
        geometry: CircleGeometryView | PolygonGeometryView
        if zone.geometry.shape == "circle":
            geometry = CircleGeometryView(
                centre=GeoPointView(
                    latitude=zone.geometry.centre.latitude,
                    longitude=zone.geometry.centre.longitude,
                ),
                radius_m=zone.geometry.radius_m,
            )
        else:
            geometry = PolygonGeometryView(
                vertices=[
                    GeoPointView(latitude=v.latitude, longitude=v.longitude)
                    for v in zone.geometry.vertices
                ]
            )
        return cls(
            zone_id=zone.zone_id,
            name=zone.name,
            kind=str(zone.kind),
            geometry=geometry,
            floor_m=zone.floor_m,
            ceiling_m=zone.ceiling_m,
        )


class SiteView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    site_id: str
    name: str
    reference: PositionView
    protected_radius_m: float
    zones: list[ZoneView]
    sensor_count: int

    @classmethod
    def from_site(cls, site: Site) -> SiteView:
        return cls(
            site_id=site.site_id,
            name=site.name,
            reference=PositionView(
                latitude=site.reference.latitude,
                longitude=site.reference.longitude,
                altitude_m=site.reference.altitude_m,
            ),
            protected_radius_m=site.protected_radius_m,
            zones=[ZoneView.from_zone(z) for z in site.zones],
            sensor_count=len(site.sensors),
        )


class SiteListResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sites: list[SiteView]
    count: int


class ScenarioCreateRequest(BaseModel):
    """A scenario definition as submitted by a client.

    Note what a client cannot send: `scenario_id`, `created_at`, `created_by`, or any notion of
    run state. Those are the server's to assign, and `extra="forbid"` rejects an attempt to
    supply them rather than ignoring it — a request that was misunderstood should fail, not
    appear to succeed (spec §14).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1024)
    seed: int = Field(ge=0)
    """Required. An unseeded scenario cannot be reproduced, and replay of it would not be
    replay of anything (ADR 0008)."""

    definition: dict[str, Any] = Field(default_factory=dict)


class ScenarioView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    name: str
    description: str
    seed: int
    definition: dict[str, Any]
    created_at: datetime
    created_by: str
    """The operator this was attributed to. Asserted, not authenticated — see FM-001."""

    @classmethod
    def from_record(cls, record: ScenarioRecord) -> ScenarioView:
        return cls(
            scenario_id=record.scenario_id,
            name=record.name,
            description=record.description,
            seed=record.seed,
            definition=record.definition,
            created_at=record.created_at,
            created_by=record.created_by,
        )


class ScenarioListResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenarios: list[ScenarioView]
    count: int


class StreamedEvent(BaseModel):
    """One event pushed over the WebSocket, addressed by its position in the log.

    `seq` is the client's resume token. A client that records the highest `seq` it processed
    can reconnect with `from_seq` set to one past it and continue exactly where it stopped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    event_id: str
    event_type: EventType
    timestamp: datetime
    source: str
    track_id: str | None

    track: TrackView | None
    """Populated when the event carries a track snapshot, projected into the same view model
    the REST endpoints return. A client therefore parses one track shape, not two."""

    detail: dict[str, Any]
    """The rest of the payload. The track snapshot is removed, since it is already above in
    the wire format rather than the domain one."""

    @classmethod
    def from_event(cls, event: Event) -> StreamedEvent:
        snapshot = event.payload.get(TRACK_SNAPSHOT_KEY)
        track = (
            TrackView.from_track(Track.model_validate(snapshot), event.seq)
            if snapshot is not None
            else None
        )
        return cls(
            seq=event.seq,
            event_id=event.event_id,
            event_type=event.event_type,
            timestamp=event.timestamp,
            source=event.source,
            track_id=event.track_id,
            track=track,
            detail={k: v for k, v in event.payload.items() if k != TRACK_SNAPSHOT_KEY},
        )
