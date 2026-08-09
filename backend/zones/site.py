"""Site model: the protected geography a deployment defends.

Config-driven and never hardcoded. No customer site, address or coordinate appears in
application code — a site is a JSON document in `config/sites/`, and the shipped one is a
fictional test site.

The site reference point anchors the local ENU frame (ADR 0004) and is immutable for the
lifetime of a running system: changing it would silently invalidate every in-flight track,
since track state is stored in that frame.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.common.geodesy import EnuPoint, GeodeticPoint, LocalFrame

SITE_SCHEMA_VERSION = "1.0.0"


class ZoneKind(StrEnum):
    """What a zone means, in escalating order of consequence.

    The ordering matters: risk assessment weights a zone by kind, and a track inside several
    overlapping zones is assessed against the most consequential one it occupies.
    """

    MONITORED = "monitored"
    """Watched airspace. Presence here is expected and unremarkable on its own."""

    RESTRICTED = "restricted"
    """Presence is notable and warrants operator attention."""

    PROTECTED = "protected"
    """The asset being defended. Presence here is the thing the system exists to notice."""

    @property
    def severity(self) -> int:
        return {"monitored": 0, "restricted": 1, "protected": 2}[self.value]


class GeoPoint(BaseModel):
    """A WGS-84 point. Altitude omitted because zone geometry is defined on the ground plane
    and vertical extent is expressed separately by the zone's altitude bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class GeodeticReference(BaseModel):
    """The site's anchor point, with altitude, unlike zone geometry vertices."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    altitude_m: float = 0.0


class CircularGeometry(BaseModel):
    """A zone defined by a centre and radius. The common case."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: Literal["circle"] = "circle"
    centre: GeoPoint
    radius_m: float = Field(gt=0.0)


class PolygonGeometry(BaseModel):
    """A zone defined by a boundary.

    Vertices are ordered and the ring is closed implicitly — the last vertex connects back to
    the first, and repeating it is rejected so there is exactly one representation of a given
    polygon.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: Literal["polygon"] = "polygon"
    vertices: list[GeoPoint] = Field(min_length=3)

    @model_validator(mode="after")
    def _reject_explicitly_closed_ring(self) -> PolygonGeometry:
        if self.vertices[0] == self.vertices[-1]:
            raise ValueError(
                "polygon ring is closed implicitly; do not repeat the first vertex as the last"
            )
        return self


class Zone(BaseModel):
    """A named region of the site with a meaning attached."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    zone_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    kind: ZoneKind
    geometry: CircularGeometry | PolygonGeometry = Field(discriminator="shape")

    floor_m: float | None = None
    """Lower altitude bound, metres above the WGS-84 ellipsoid. None means unbounded below."""

    ceiling_m: float | None = None
    """Upper altitude bound. None means unbounded above — the usual case, since an object
    that overflies a protected zone at altitude is still overflying it."""

    @model_validator(mode="after")
    def _floor_below_ceiling(self) -> Zone:
        if self.floor_m is not None and self.ceiling_m is not None:
            if self.floor_m >= self.ceiling_m:
                raise ValueError(
                    f"zone {self.zone_id}: floor_m ({self.floor_m}) must be below "
                    f"ceiling_m ({self.ceiling_m})"
                )
        return self


class SensorPlacement(BaseModel):
    """Where a sensor sits and which way it faces.

    Placement only. A sensor's detection characteristics — range, field of view, update rate,
    uncertainty, probability of detection, false alarm rate — live in the sensor registry,
    because the same sensor model can be placed at many sites and the same site can be fitted
    with different sensors.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    registry_key: str = Field(min_length=1, max_length=64)
    """Which entry in the sensor registry describes this sensor's characteristics."""

    position: GeoPoint
    height_m: float = 0.0
    """Height of the sensor above the site reference altitude — mast or rooftop mounting."""

    boresight_deg: float = Field(default=0.0, ge=0.0, lt=360.0)
    """Direction the sensor faces, degrees clockwise from true north. Meaningless for
    sensors with full azimuthal coverage, and ignored by those sensor models."""

    enabled: bool = True


class Site(BaseModel):
    """A complete site definition. Loaded from `config/sites/`, never constructed in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SITE_SCHEMA_VERSION
    site_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)

    reference: GeodeticReference
    """Anchor of the local ENU frame. Immutable while the system is running."""

    protected_radius_m: float = Field(gt=0.0)
    """Radius of the primary protected area around the reference point. Retained as a
    top-level field because the specification treats it as the site's headline figure, and
    because risk assessment needs one unambiguous distance-to-protected-area even when no
    explicit protected zone has been configured."""

    zones: list[Zone] = Field(default_factory=list)
    sensors: list[SensorPlacement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> Site:
        for label, ids in (
            ("zone_id", [z.zone_id for z in self.zones]),
            ("sensor_id", [s.sensor_id for s in self.sensors]),
        ):
            duplicates = {i for i in ids if ids.count(i) > 1}
            if duplicates:
                raise ValueError(f"duplicate {label} in site {self.site_id}: {sorted(duplicates)}")
        return self

    def local_frame(self) -> LocalFrame:
        """The ENU frame anchored at this site's reference point."""
        return LocalFrame(
            GeodeticPoint(
                latitude_deg=self.reference.latitude,
                longitude_deg=self.reference.longitude,
                altitude_m=self.reference.altitude_m,
            )
        )

    def sensor_positions_enu(self) -> dict[str, EnuPoint]:
        """Each enabled sensor's position in the local frame."""
        frame = self.local_frame()
        return {
            sensor.sensor_id: frame.to_enu(
                GeodeticPoint(
                    latitude_deg=sensor.position.latitude,
                    longitude_deg=sensor.position.longitude,
                    altitude_m=self.reference.altitude_m + sensor.height_m,
                )
            )
            for sensor in self.sensors
            if sensor.enabled
        }

    def zone(self, zone_id: str) -> Zone:
        for z in self.zones:
            if z.zone_id == zone_id:
                return z
        raise KeyError(f"no zone {zone_id!r} in site {self.site_id}")
