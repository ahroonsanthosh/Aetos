"""Where a camera is and what it can see.

A camera measures where in its image something moved, and nothing else. Turning that into a
position needs the mount (where the camera is and which way it points) and the lens (how many
degrees the frame spans), and it needs a range the camera cannot measure. All of it is declared
here, by whoever set the camera up, and none of it is guessed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CameraMount(BaseModel):
    """One fixed camera's placement, lens and assumed range."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    altitude_m: float
    """Lens height, metres above the WGS-84 ellipsoid like every other altitude (FM-004)."""

    boresight_deg: float = Field(ge=0.0, lt=360.0)
    """True bearing of the image centre, degrees clockwise from north."""

    tilt_deg: float = Field(default=0.0, ge=-30.0, le=80.0)
    """Elevation of the image centre above the horizon. Positive points up."""

    horizontal_fov_deg: float = Field(default=66.0, gt=5.0, lt=170.0)
    """Full horizontal field of view. 66 degrees is a typical phone main camera."""

    vertical_fov_deg: float = Field(default=40.0, gt=3.0, lt=150.0)

    assumed_range_m: float = Field(default=300.0, gt=10.0, le=5000.0)
    """Where along the bearing the contact is placed. A monocular camera measures no range, so
    this is a declared assumption and `range_sigma_m` says how little it means."""

    range_sigma_m: float = Field(default=250.0, gt=0.0, le=5000.0)
    """One-sigma uncertainty of that assumed range. Of the same order as the range itself,
    because that is the honest size of what a bearing alone says about distance."""

    @model_validator(mode="after")
    def _range_sigma_is_bounded(self) -> CameraMount:
        if self.range_sigma_m > 2.0 * self.assumed_range_m:
            raise ValueError("range_sigma_m may not exceed twice assumed_range_m")
        return self
