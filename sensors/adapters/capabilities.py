"""What a source declares about itself, and the floors that declaration is measured against.

`SensorHealth` says what a sensor is doing right now. This says what it *is*: coverage, timing,
accuracy, which dimensions it actually measures, and how trustworthy the wire between it and
this process is. Static for the life of a run, cheap to call, and meaningful before anything is
connected — which is when a customer wants to see it.

The two are deliberately separate. Folding capability into health would make a declared
specification appear to change when a cable came loose, and would make the declaration
unavailable at exactly the moment it is most useful.

## Why this exists

Aetos propagates uncertainty faithfully and adds none of its own, so the operator picture is
exactly as good as the sensors feeding it. That makes the entry requirement **honesty rather
than accuracy**: a source reporting an honest 15-metre error is usable, because the gate widens,
the filter weights it lightly and the operator sees a larger ellipse. A source claiming two
metres and delivering fifteen is unusable at any accuracy, because the filter believes it.

See `docs/sensor_interface_specification.md` for the contract in full and
`docs/adr/0017-sensor-capability-declaration.md` for why it is shaped this way.

## The floors are computed, never written down

`FloorThresholds` carries no defaults. Every threshold is derived from the deployment's own
tracking constants and site geometry by the caller — `backend/tracking/requirements.py` does
this for Aetos itself. A number written into this module would be a second copy of a value
`backend/tracking` owns, and the two would stop agreeing the first time anything was retuned.

This module therefore imports nothing from `backend` at all. An adapter must not know what a
track is (`sensors/adapters/README.md`), and a module adapters declare themselves through must
not smuggle that knowledge in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sensors.schemas.detection import SensorType

SENSOR_CAPABILITIES_SCHEMA_VERSION = "1.0.0"
"""Version of the declaration format itself. Bumped alongside the interface specification."""


class SensorFloor(IntEnum):
    """How much a source can be relied on to do, as an ordinal ladder.

    Ordinal rather than pass/fail because the failure modes are genuinely graded. A source that
    cannot sustain a track alone may still contribute usefully to one another sensor is holding,
    and a boolean would either exclude it or admit it with no way to state the limit.

    `CLASSIFYING` is deliberately not a rung here — identity evidence and position quality are
    orthogonal, and it is carried as a separate flag on the assessment.

    **`REJECTED` is zero on purpose.** The specification numbers the admitted floors 0, 1 and 2,
    and the obvious encoding would give `INGESTIBLE` the value zero — which would make the
    lowest *passing* grade falsy, so that `if assessment.floor:` silently treats an admitted
    sensor as a rejected one. Offsetting by one puts the only value for which falsiness could
    ever be the right reading at zero, and `rung` gives back the specification's numbering for
    display. A trap that cannot be sprung is better than one that is documented.
    """

    REJECTED = 0
    """Does not reach the ingestible floor. Not admitted; nothing is ingested."""

    INGESTIBLE = 1
    """Produces valid canonical detections with honest uncertainty. Appears on the picture."""

    TRACKABLE = 2
    """Forms and holds confirmed tracks unaided. A single-sensor site works."""

    FUSABLE = 3
    """Reconciles with other sensors observing the same object."""

    @property
    def label(self) -> str:
        return self.name.lower()

    @property
    def rung(self) -> int | None:
        """The floor's number as `docs/sensor_interface_specification.md` writes it, or None for
        a rejected source, which has no rung."""
        return None if self is SensorFloor.REJECTED else self.value - 1

    @property
    def is_admitted(self) -> bool:
        return self is not SensorFloor.REJECTED


class TransportIntegrity(StrEnum):
    """How much the link between the sensor and this process can be trusted not to lie.

    A declaration most sensor specifications omit, and the difference between a link that fails
    loudly and one that fails quietly. See ADR 0018.
    """

    VERIFIED = "verified"
    """Corruption is detected by a checksum or equivalent at or below this layer — TCP, TLS, a
    protocol carrying a CRC, or an in-process source with no wire at all."""

    STRUCTURAL = "structural"
    """No checksum. Corruption is caught only by the frame's own structure and by the
    plausibility of the values it carries. Admissible, and common on cheap serial hardware —
    but a source declaring this must also declare its residual corruption probability."""

    NONE = "none"
    """No framing and no integrity. Corruption is undetectable."""


class Coverage(BaseModel):
    """What a sensor can see, geometrically. Outside this, no detection is produced.

    Elevation bounds are angles above the horizontal *at the sensor*, so a sensor on a mast still
    cannot see an object directly overhead unless its upper bound says it can.

    Lives here rather than with the simulated sensor model because coverage is a property every
    source declares, not a simulation parameter. The simulated model imports it from here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_range_m: float = Field(gt=0.0)
    min_range_m: float = Field(default=0.0, ge=0.0)
    """Blind zone. A real pulsed sensor cannot resolve returns closer than its own pulse
    length, and a scenario that flies an object over the sensor should exercise that."""

    azimuth_extent_deg: float = Field(gt=0.0, le=360.0)
    """Total width of the azimuth sector, centred on the placement's boresight. 360 means full
    azimuthal coverage, in which case boresight is meaningless and is ignored."""

    elevation_min_deg: float = Field(ge=-90.0, le=90.0)
    elevation_max_deg: float = Field(ge=-90.0, le=90.0)

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> Coverage:
        if self.min_range_m >= self.max_range_m:
            raise ValueError("min_range_m must be below max_range_m")
        if self.elevation_min_deg >= self.elevation_max_deg:
            raise ValueError("elevation_min_deg must be below elevation_max_deg")
        return self

    @property
    def has_full_azimuth(self) -> bool:
        return self.azimuth_extent_deg >= 360.0

    @property
    def elevation_extent_deg(self) -> float:
        """Total vertical aperture. Used to bound the vertical uncertainty of a source with no
        elevation channel, which assumes a target lies in the sensor's own plane (ADR 0019)."""
        return self.elevation_max_deg - self.elevation_min_deg


class TimingDeclaration(BaseModel):
    """When observations happen, how fast they arrive, and how well that time is known.

    Three separate quantities that are routinely conflated. Update rate is how often the sensor
    looks; latency is how long a look takes to reach this process and *can be corrected for*;
    timestamp uncertainty is how wrong the reported observation time is and **cannot be**.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    update_rate_hz: float = Field(gt=0.0, le=10_000.0)
    """Revisit rate on any bearing within coverage. For a mechanically swept sensor this is the
    sweep's revisit interval, not the rate at which frames arrive while the beam is on target —
    the tracker cares how often a given object is observed, not how busy the link is."""

    max_latency_ms: float = Field(ge=0.0)
    """Worst-case observation-to-`get_detections()` delay."""

    timestamp_uncertainty_ms: float = Field(ge=0.0)
    """One-sigma error on the reported observation time.

    The quantity that decides whether a source can be fused. Timing error becomes position error
    at the target's speed, and unlike random measurement error it is systematic, so it does not
    average out across updates."""

    @property
    def update_period_s(self) -> float:
        return 1.0 / self.update_rate_hz


class AccuracyDeclaration(BaseModel):
    """How wrong the reported position is, and how that error is shaped.

    Anisotropy is declared rather than implied. Nearly every ranging sensor is far more precise
    along the line of sight than across it, and a scalar uncertainty covering both is a
    simplification that costs exactly the discrimination which keeps two crossing tracks apart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    position_uncertainty_m: float = Field(gt=0.0)
    """One-sigma on the worst axis, at `reference_range_m`. Never an optimistic summary: a
    consumer reading only this figure must not end up believing the sensor is better than it is
    in its weakest direction."""

    reference_range_m: float = Field(gt=0.0)
    """The range `position_uncertainty_m` applies at. Required rather than defaulted, because an
    accuracy figure with no range attached is not a statement about anything."""

    angular_uncertainty_mrad: float = Field(default=0.0, ge=0.0)
    """Cross-range error that grows with range, if this source has one — an angular error
    subtends more metres the further away the object is.

    Zero means the declared figure is range-independent, which is true of very few real sensors
    and should be left at zero only when it genuinely is."""

    horizontal_uncertainty_m: float | None = Field(default=None, gt=0.0)
    """One-sigma on the worst *horizontal* axis at `reference_range_m`, when it differs from the
    worst axis overall.

    Exists because of a perverse result the worst-axis figure produced on its own. A source with
    no elevation channel assumes an altitude and declares a large vertical uncertainty to say how
    little that assumption is worth (ADR 0019) — which is exactly the honest thing to do, and
    which made the worst axis the assumption rather than any measurement. Judged on that, such a
    source was failed for the size of a limitation it had correctly declared, while one that said
    nothing about altitude passed. Under a floor whose whole premise is that honesty is the entry
    requirement, that is backwards.

    So the trackability requirement is judged horizontally, which is also what it is actually
    about: `protected_radius_m` is a horizontal distance, and whether an object is inside a zone
    footprint is a horizontal question. The vertical story is carried by `reports_altitude` and by
    the covariance, where it belongs.

    None means the source has no separate horizontal figure and the worst-axis one is used —
    correct for any sensor that genuinely measures all three dimensions, which is most of them.
    """

    horizontal_angular_uncertainty_mrad: float | None = Field(default=None, ge=0.0)
    """Cross-range growth of the horizontal error, when it differs from `angular_uncertainty_mrad`.

    Separate for the same reason: for a source that assumes altitude, the worst-axis angular term
    is the assumption growing with range, not the bearing error. None falls back to
    `angular_uncertainty_mrad`.
    """

    anisotropy_ratio: float = Field(default=1.0, ge=1.0)
    """Worst-axis one-sigma divided by best-axis. 1.0 asserts isotropic error.

    A source declaring a ratio above 2.0 must populate `position_covariance_enu`, because a
    scalar cannot express an ellipse that elongated and fusion would weight it wrongly in the
    axis it was actually good at."""

    reports_covariance: bool = False
    """Whether emitted detections carry a full 3x3 ENU covariance."""

    def uncertainty_at_m(self, range_m: float) -> float:
        """One-sigma worst-axis at an arbitrary range.

        The declared figure is treated as the floor and the angular term grows on top of it,
        combined in quadrature because the two contributions are independent. With no angular
        term declared the figure is constant, which is what a range-independent source means.
        """
        if self.angular_uncertainty_mrad <= 0.0:
            return self.position_uncertainty_m

        floor = self.range_independent_floor_m()
        angular = range_m * self.angular_uncertainty_mrad * 1e-3
        return math.hypot(floor, angular)

    def horizontal_uncertainty_at_m(self, range_m: float) -> float:
        """One-sigma worst *horizontal* axis at an arbitrary range.

        Falls back to the worst-axis figure when no horizontal one is declared, so a source that
        measures all three dimensions needs to declare nothing extra and is judged exactly as it
        was before this existed.
        """
        if self.horizontal_uncertainty_m is None:
            return self.uncertainty_at_m(range_m)

        angular_mrad = (
            self.horizontal_angular_uncertainty_mrad
            if self.horizontal_angular_uncertainty_mrad is not None
            else self.angular_uncertainty_mrad
        )
        if angular_mrad <= 0.0:
            return self.horizontal_uncertainty_m

        angular_at_reference = self.reference_range_m * angular_mrad * 1e-3
        residual = self.horizontal_uncertainty_m**2 - angular_at_reference**2
        floor = math.sqrt(residual) if residual > 0.0 else 0.0
        return math.hypot(floor, range_m * angular_mrad * 1e-3)

    def range_independent_floor_m(self) -> float:
        """The part of the declared uncertainty that does not grow with range.

        Recovered from the declaration rather than declared separately: the figure given at
        `reference_range_m` already contains the angular term at that range, so subtracting it in
        quadrature yields the floor. Clamped at zero for a declaration whose angular term alone
        would exceed the stated total, which is a self-inconsistent declaration the conformance
        suite reports separately rather than something to fail on here.
        """
        angular_at_reference = self.reference_range_m * self.angular_uncertainty_mrad * 1e-3
        residual = self.position_uncertainty_m**2 - angular_at_reference**2
        return math.sqrt(residual) if residual > 0.0 else 0.0


class DimensionsDeclaration(BaseModel):
    """Which quantities this source genuinely measures.

    Absence is a first-class answer and is not disqualifying. What is disqualifying is silence:
    a source that does not measure altitude must say so, because it will still populate
    `altitude_m` — the canonical schema requires it — and the figure it puts there is an
    assumption. See ADR 0019.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reports_range: bool
    reports_bearing: bool

    reports_elevation: bool = False
    """Whether the sensor has an elevation channel at all."""

    reports_altitude: bool = False
    """Whether `position.altitude_m` is a measurement.

    False means every emitted detection carries `altitude_source = assumed` and a vertical
    variance wide enough to say so numerically. The conformance suite checks that the two
    agree."""

    reports_radial_velocity: bool = False
    """Range rate — genuinely measurable by a Doppler-capable source, unlike speed or heading."""

    @model_validator(mode="after")
    def _altitude_needs_elevation(self) -> DimensionsDeclaration:
        if self.reports_altitude and not self.reports_elevation:
            raise ValueError(
                "reports_altitude is true while reports_elevation is false: a source with no "
                "elevation channel cannot measure altitude, and declaring otherwise would let "
                "an assumed figure be rendered as a measurement (ADR 0019)"
            )
        return self

    @model_validator(mode="after")
    def _position_needs_range_and_bearing(self) -> DimensionsDeclaration:
        if not (self.reports_range or self.reports_bearing):
            raise ValueError(
                "a source reporting neither range nor bearing cannot produce a position; it is "
                "below the ingestible floor and has nothing to declare"
            )
        return self


class TransportDeclaration(BaseModel):
    """The link, and how much it can be trusted.

    See ADR 0018. The short version: integrity cannot be retrofitted onto a one-way stream that
    has none, so a source on such a link declares the residual risk as a computed number and the
    system degrades it on measured evidence rather than on optimism.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1, max_length=32)
    """How the source is reached — `simulated`, `serial`, `tcp`, `udp`, `http`, and so on.
    Free-form because the set is open and nothing branches on it; it is for a human reading a
    declaration."""

    integrity: TransportIntegrity

    residual_corruption_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    """Probability that a single corrupted byte yields a frame the decoder accepts as valid.

    A property of the frame layout and the configured plausibility envelope, so it is calculated
    by the decoder rather than estimated by hand. Required for a `structural` link and
    meaningless for the others."""

    @model_validator(mode="after")
    def _structural_links_declare_their_residual(self) -> TransportDeclaration:
        if self.integrity is TransportIntegrity.STRUCTURAL:
            if self.residual_corruption_probability is None:
                raise ValueError(
                    "a structural-integrity link must declare residual_corruption_probability: "
                    "the plausibility envelope is the only integrity check such a link has, and "
                    "how much it lets through is the whole question (ADR 0018)"
                )
        elif self.residual_corruption_probability is not None:
            raise ValueError(
                f"residual_corruption_probability is meaningless for a {self.integrity.value!r} "
                "link and must be omitted; declaring one implies a structural check that is not "
                "what is protecting this transport"
            )
        return self


class SensorCapabilities(BaseModel):
    """One source's complete self-declaration.

    Everything a customer, an integrator or the conformance suite needs in order to know what
    this source is, before it has produced a single detection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SENSOR_CAPABILITIES_SCHEMA_VERSION
    sensor_id: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType
    description: str = Field(min_length=1, max_length=512)

    coverage: Coverage
    timing: TimingDeclaration
    accuracy: AccuracyDeclaration
    dimensions: DimensionsDeclaration
    transport: TransportDeclaration

    classifies: bool = False
    """Whether this source contributes identity evidence. Optional forever: the classification
    module must produce a defensible label with no sensor hint at all (ADR 0002)."""

    coverage_validated: bool = False
    """Whether the declared coverage has been measured against the threat class this deployment
    cares about, rather than taken from a datasheet.

    Defaults to false and should stay false until someone has flown the actual airframe at known
    ranges. A datasheet range is quoted against a radar cross-section that is usually one to two
    orders of magnitude larger than a small multirotor, and until that is checked, silence from
    this sensor must not be rendered as clear sky (FM-016)."""


# --------------------------------------------------------------------------------------------
# Floors
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FloorThresholds:
    """What a source must clear to reach each floor, in this deployment.

    Every field is derived from something the deployment already owns — the tracker's lifecycle
    tuning, the association gate, the pipeline cadence, the site's protected radius. There are
    no defaults, deliberately: a default here would be a second copy of a number
    `backend/tracking` owns, and the two would stop agreeing the first time anything was
    retuned. `backend/tracking/requirements.py` derives these for Aetos.
    """

    update_period_ceiling_s: float
    """Slowest update that can still produce a *confirmed* track.

    Bounded by tentative-track retirement, not by the coast window, and the difference is large.
    A confirmed track coasts on elapsed time and survives a long gap; a tentative one is retired
    after a fixed number of missed cycles, so a slow source is retired before it ever accumulates
    the detections confirmation requires. Forming a track is far harder than holding one."""

    latency_ceiling_ms: float
    """Beyond one update period the picture is always more than one update stale, and prediction
    is extrapolating from a position that has already been superseded."""

    position_uncertainty_ceiling_m: float
    """Uncertainty at which a position stops being a useful input to a zone determination.

    Derived from the scale of the decisions being made rather than from the sensor: a positional
    error that is a large fraction of the protected area makes "inside or outside" a coin
    toss."""

    timestamp_uncertainty_ceiling_ms: float
    """Above this, timing error dominates position error and two sensors observing one object
    place it in two places (FM-023)."""

    gate_radius_sigmas: float
    """Square root of the association gate's chi-squared threshold. Used to compute
    discrimination range, not to gate a floor."""

    minimum_separation_m: float
    """How far apart two objects the site must keep distinct may be. A site assumption, not a
    derived fact, and the figure most worth revisiting per deployment."""

    decision_scale_m: float
    """What `position_uncertainty_ceiling_m` was derived from — the site's protected radius.
    Carried so a report can explain its own threshold instead of asserting a number."""


@dataclass(frozen=True, slots=True)
class FloorAssessment:
    """Which floor a declaration reaches, and what stopped it going higher."""

    floor: SensorFloor
    """Never None. `SensorFloor.REJECTED` is the explicit bottom, so no caller has to remember
    that an absent floor and the lowest floor are different things."""

    classifying: bool
    shortfalls: tuple[str, ...]
    """Why the next floor was not reached, in words, one entry per unmet requirement. Empty at
    FUSABLE. Always populated when a floor was missed — a floor with no explanation cannot be
    acted on."""

    assessed_range_m: float
    """The range the uncertainty requirement was judged at.

    The decision scale, or the sensor's own maximum range when that is nearer. Carried on the
    assessment rather than recomputed by anything that reports it: a report showing the error at
    a different range from the one the verdict used would read as contradicting its own
    conclusion, which is worse than not showing it at all.
    """

    discrimination_range_m: float
    """Furthest range at which two objects `minimum_separation_m` apart remain separable.

    Reported rather than gated on, because a sensor that cannot separate close targets still
    tracks single ones perfectly well. It is often far shorter than the detection range, and it
    is the figure a customer is least likely to have been told: a radar detecting to eight
    kilometres may only discriminate to a few hundred metres.

    Zero means the source cannot separate objects at that separation at any range."""

    def reaches(self, floor: SensorFloor) -> bool:
        return self.floor >= floor


def assessed_range_m(
    capabilities: SensorCapabilities, thresholds: FloorThresholds
) -> float:
    """The range the uncertainty requirement is judged at.

    The decision scale, or the sensor's own maximum range when that is nearer. Extrapolating a
    short-range sensor's angular error out to a site radius it cannot reach would compute an
    uncertainty for a detection it can never produce, and fail it on that — rejecting every
    close-in sensor at a large site for a reason that does not exist.
    """
    return min(thresholds.decision_scale_m, capabilities.coverage.max_range_m)


def assess_floor(
    capabilities: SensorCapabilities, thresholds: FloorThresholds
) -> FloorAssessment:
    """Evaluate a declaration against a deployment's thresholds.

    Purely a reading of what was declared. It establishes that the *claims* clear the floors, not
    that they are true — no test can determine that a sensor claiming three metres delivers it,
    which requires measurement against a known reference. The distinction is the point of the
    honesty framing and is stated in every report the conformance suite prints.
    """
    ingestible_shortfalls = _ingestible_shortfalls(capabilities)
    if ingestible_shortfalls:
        return FloorAssessment(
            floor=SensorFloor.REJECTED,
            classifying=capabilities.classifies,
            shortfalls=tuple(ingestible_shortfalls),
            assessed_range_m=assessed_range_m(capabilities, thresholds),
            discrimination_range_m=_discrimination_range(capabilities, thresholds),
        )

    trackable_shortfalls = _trackable_shortfalls(capabilities, thresholds)
    if trackable_shortfalls:
        return FloorAssessment(
            floor=SensorFloor.INGESTIBLE,
            classifying=capabilities.classifies,
            shortfalls=tuple(trackable_shortfalls),
            assessed_range_m=assessed_range_m(capabilities, thresholds),
            discrimination_range_m=_discrimination_range(capabilities, thresholds),
        )

    fusable_shortfalls = _fusable_shortfalls(capabilities, thresholds)
    floor = SensorFloor.TRACKABLE if fusable_shortfalls else SensorFloor.FUSABLE

    return FloorAssessment(
        floor=floor,
        classifying=capabilities.classifies,
        shortfalls=tuple(fusable_shortfalls),
        assessed_range_m=assessed_range_m(capabilities, thresholds),
        discrimination_range_m=_discrimination_range(capabilities, thresholds),
    )


def _ingestible_shortfalls(capabilities: SensorCapabilities) -> list[str]:
    """Floor 0. Almost everything here is enforced by the models above, so this catches the two
    things a valid declaration can still get wrong."""
    shortfalls: list[str] = []

    dimensions = capabilities.dimensions
    if not (dimensions.reports_range and dimensions.reports_bearing):
        shortfalls.append(
            "R0.2: a position needs both range and bearing; this source declares "
            f"range={dimensions.reports_range}, bearing={dimensions.reports_bearing}, so it "
            "cannot place an object without another sensor to cross-fix against"
        )

    if capabilities.transport.integrity is TransportIntegrity.NONE:
        shortfalls.append(
            "R0.3: transport integrity is 'none', so a corrupted value is indistinguishable "
            "from a measurement and no uncertainty this source declares can be relied on"
        )

    return shortfalls


def _trackable_shortfalls(
    capabilities: SensorCapabilities, thresholds: FloorThresholds
) -> list[str]:
    """Floor 1. Update rate and latency, plus a sanity bound on uncertainty at decision scale.

    Note what is *not* here: a discrimination requirement. A large positional uncertainty widens
    the association gate, which never prevents a track associating with itself — it prevents
    telling two close objects apart. That is a range-dependent quality figure, reported on the
    assessment, not an entry requirement.
    """
    shortfalls: list[str] = []
    timing = capabilities.timing

    if timing.update_period_s > thresholds.update_period_ceiling_s:
        shortfalls.append(
            f"R1.1: updates every {timing.update_period_s:.2f} s "
            f"({timing.update_rate_hz:.2f} Hz), slower than the "
            f"{thresholds.update_period_ceiling_s:.2f} s ceiling. A track would be retired as "
            "unsupported before it accumulated enough detections to be confirmed, so this "
            "source produces tentative tracks that never promote"
        )

    if timing.max_latency_ms > thresholds.latency_ceiling_ms:
        shortfalls.append(
            f"R1.3: worst-case latency {timing.max_latency_ms:.0f} ms exceeds the "
            f"{thresholds.latency_ceiling_ms:.0f} ms ceiling, so the picture is always more "
            "than one update stale"
        )

    # Horizontal, not worst-axis. `protected_radius_m` is a horizontal distance and whether an
    # object is inside a zone footprint is a horizontal question — and judging on the worst axis
    # penalised a source for honestly declaring how uncertain its assumed altitude was, which is
    # backwards under a floor built on honesty. See `AccuracyDeclaration.horizontal_uncertainty_m`.
    judged_at = assessed_range_m(capabilities, thresholds)
    uncertainty_at_scale = capabilities.accuracy.horizontal_uncertainty_at_m(judged_at)

    if uncertainty_at_scale > thresholds.position_uncertainty_ceiling_m:
        shortfalls.append(
            f"R1.2: horizontal one-sigma of {uncertainty_at_scale:.1f} m at {judged_at:.0f} m "
            f"exceeds the {thresholds.position_uncertainty_ceiling_m:.1f} m ceiling for a "
            f"{thresholds.decision_scale_m:.0f} m decision scale; a zone determination at that "
            "uncertainty is not a determination"
        )

    return shortfalls


def _fusable_shortfalls(
    capabilities: SensorCapabilities, thresholds: FloorThresholds
) -> list[str]:
    """Floor 2. Almost entirely a clock and covariance problem."""
    shortfalls: list[str] = []

    if capabilities.timing.timestamp_uncertainty_ms > thresholds.timestamp_uncertainty_ceiling_ms:
        shortfalls.append(
            f"R2.1: timestamp uncertainty {capabilities.timing.timestamp_uncertainty_ms:.0f} ms "
            f"exceeds the {thresholds.timestamp_uncertainty_ceiling_ms:.0f} ms ceiling. Timing "
            "error is systematic and becomes position error at target speed, so two sensors "
            "observing one object would place it in two places"
        )

    accuracy = capabilities.accuracy
    if accuracy.anisotropy_ratio > 2.0 and not accuracy.reports_covariance:
        shortfalls.append(
            f"R2.2: error is {accuracy.anisotropy_ratio:.1f}:1 anisotropic but no covariance is "
            "reported. Fusion weights by covariance, so a scalar would contribute this source's "
            "worst axis to the axis it was actually good at"
        )

    return shortfalls


def _discrimination_range(
    capabilities: SensorCapabilities, thresholds: FloorThresholds
) -> float:
    """Furthest range at which two objects `minimum_separation_m` apart stay separable.

    Two association gates must not each reach the other's detection, so each gate radius has to
    stay inside half the separation:

        gate_radius_sigmas * sigma(r)  <=  separation / 2

    Solved for `r` rather than evaluated at one range, because cross-range error grows with
    range and the answer is a distance, not a yes or no. Capped at the declared maximum range:
    discrimination beyond what the sensor can detect is not a capability.
    """
    ceiling = thresholds.minimum_separation_m / (2.0 * thresholds.gate_radius_sigmas)
    accuracy = capabilities.accuracy
    max_range = capabilities.coverage.max_range_m

    floor_sigma = accuracy.range_independent_floor_m()
    if floor_sigma >= ceiling:
        # The range-independent part alone already exceeds what discrimination allows, so there
        # is no range at which it works — including at zero.
        return 0.0

    if accuracy.angular_uncertainty_mrad <= 0.0:
        # Range-independent and inside the ceiling: it discriminates everywhere it can see.
        return max_range

    # sigma(r)^2 = floor^2 + (r * angular)^2 <= ceiling^2
    angular = accuracy.angular_uncertainty_mrad * 1e-3
    limit = math.sqrt(ceiling**2 - floor_sigma**2) / angular
    return min(limit, max_range)
