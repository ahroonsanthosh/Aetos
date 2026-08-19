"""Deployment configuration for the UART FMCW radar adapter.

Two config objects, deliberately kept separate:

- `SerialLinkConfig` is about the wire — which port, how fast, how patient to be with a link
  that drops.
- `RadarMountConfig` is about where the unit is pointed and how much to trust what it reports.

Neither belongs in `config/sensor_registry/`. That directory's schema
(`sensors.adapters.simulated.sensor_model.SensorModel`) describes a *simulated* sensor's noise
model for the truth-degrading simulator — coverage volume, probability of detection, false
alarm rate — not a real unit's serial port or mount geometry, and its loader
(`sensors/adapters/simulated/registry.py`) validates every entry strictly against that shape.
Forcing this adapter's config into it would blur a distinction the loader depends on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SerialLinkConfig:
    """How to reach the module over UART."""

    port: str
    """OS device path, e.g. `/dev/ttyUSB0` (Linux/macOS) or `COM5` (Windows)."""

    baud_rate: int = 115200
    read_timeout_s: float = 0.05
    reconnect_interval_s: float = 2.0
    """Minimum time between reconnect attempts once the link is down. Keeps a missing or
    unplugged sensor from having `open()` retried every single poll cycle."""


@dataclass(frozen=True, slots=True)
class RadarMountConfig:
    """Where the unit is pointed, and how much to trust what it reports.

    Every uncertainty and calibration field below is a documented placeholder, not a measured
    value: this hardware has not been through a calibration pass against known ranges and
    bearings. Tighten them once it has. Loosening a placeholder silently is the wrong direction
    to be wrong in for a system feeding a threat-assessment pipeline, so each one below defaults
    conservative (wide) rather than optimistic.

    See this package's README for the reasoning behind each placeholder and how to verify it in
    the field.
    """

    boresight_deg: float
    """Compass heading (degrees clockwise from true north) the sensor's front face points."""

    tilt_deg: float = 0.0
    """Physical mount elevation angle, degrees above horizontal. This module reports no
    elevation of its own, so every detection's height is *inferred* from this one fixed angle,
    not measured — see `elevation_uncertainty_deg`."""

    min_valid_range_m: float = 1.0
    max_valid_range_m: float = 100.0
    """Matches the module's rated maximum. The wire's distance byte can encode up to 255 m;
    beyond the rated range the reading is unreliable rather than merely far away, so it is
    dropped rather than passed through with a wide error bar."""

    min_signal_raw: int = 0
    """Targets reporting a signal byte below this are dropped as noise-floor returns, rather
    than emitted with an honestly-low confidence. 0 disables the gate."""

    range_sigma_m: float = 2.0
    """One-sigma along-range uncertainty. Placeholder pending a calibration pass."""

    azimuth_uncertainty_deg: float = 8.0
    """One-sigma angular uncertainty, converted to a cross-range distance at each detection's
    own range. Placeholder — this module's angle output is coarse and its true resolution is
    not documented in the sources available when this adapter was written."""

    elevation_uncertainty_deg: float = 20.0
    vertical_sigma_floor_m: float = 5.0
    """`elevation_uncertainty_deg` stands in for the fact that height is inferred from
    `tilt_deg` and never actually measured — deliberately wide, not a genuine angular
    resolution figure. `vertical_sigma_floor_m` keeps close-range detections from reporting an
    implausibly small vertical uncertainty just because the angular term shrinks with range."""

    confidence_signal_floor_raw: int = 10
    confidence_signal_ceiling_raw: int = 220
    confidence_min: float = 0.15
    confidence_max: float = 0.85
    """The module's per-target signal byte is mapped linearly from
    `[confidence_signal_floor_raw, confidence_signal_ceiling_raw]` into
    `[confidence_min, confidence_max]`. This is a relative proxy for return strength, not a
    calibrated detection probability — see the package README before treating
    `Quality.confidence` from this adapter as comparable to a calibrated source's."""

    direction_zero_means_approaching: bool = True
    """Polarity of the wire's direction byte, mapped to Aetos's `radial_velocity_mps` sign
    convention (negative = approaching, positive = receding). UNVERIFIED against the vendor's
    own protocol document — reverse-derived from a community reference implementation instead.
    Flip this if a field test (walk a target toward, then away from, the mounted unit) shows
    the resulting Detections have the wrong sign."""
