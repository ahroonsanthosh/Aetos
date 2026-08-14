"""The `RadarSensor` contract — everything a hardware radar unit must provide, and no more.

This is a **second, inner boundary**, one level below `sensors/adapters/interface.py`. The
outer boundary (`SensorAdapter`, canonical `Detection`) is what makes the simulator and a real
sensor interchangeable to the rest of Aetos. This one exists for a narrower reason: the radar
unit currently on the bench is a *testbed*, explicitly expected to be replaced, and swapping it
must not touch anything but its own driver.

So the layering is:

    hardware  ->  RadarSensor          ->  HardwareRadarAdapter  ->  Detection  ->  Aetos
                  (this file)              (sensors/adapters/         (canonical)
                                            hardware_radar/)

`TargetDetection` is deliberately *not* the canonical `Detection`. A radar unit reports what it
measures — a range, an angle off its own boresight, a range rate — in its own frame, and it
knows nothing about WGS-84, ellipsoidal altitude, site geometry or detection identifiers.
Making the driver produce canonical Detections directly would force every future driver to
re-implement geodesy and mount geometry, and would put the temptation to invent an altitude
inside the one component least equipped to do it honestly.

What crosses this boundary is therefore the smallest honest description of a radar return.
What crosses the *outer* boundary is a canonical Detection, and only the adapter builds one.

## Replacing the hardware

A new unit means a new implementation of `RadarSensor` and nothing else. Concretely, on a
hardware swap the two driver modules in this package are replaced — the protocol codec and the
driver built on it — and `config.py` gains or loses fields. `geometry.py`, the adapter, and every
module downstream of it are untouched.

`tests/unit/test_module_boundaries.py` enforces that the current unit's model designation never
appears outside those two modules, so the moment driver-specific knowledge starts leaking into
shared code, a test says so by name. That check is why this file describes the hardware only in
general terms: this is the layer meant to outlive it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class RadarSensorError(RuntimeError):
    """A radar unit was used in a way its lifecycle does not permit, or failed irrecoverably.

    Raised rather than absorbed, for the same reason `SensorAdapterError` is: a driver that
    quietly returns no targets when it is unconfigured, unplugged or mis-wired looks exactly
    like a radar observing empty sky, and that is the one thing an operator must never be shown
    by mistake (FM-010).

    Note what is *not* an error: a poll that finds nothing. That returns an empty list.
    """


class RadarConnectionState(StrEnum):
    """The driver's knowledge of its own link to the hardware, in one word.

    About the *link*, not about the sky. Whether a connected radar that has reported nothing
    for a minute is broken or merely looking at an empty approach is not a question this layer
    answers — the adapter reports the facts upward and `backend/system_health` judges them.
    """

    DISCONNECTED = "disconnected"
    """No serial port open. The initial state, and the state after `disconnect()`."""

    CONNECTED = "connected"
    """Port open and readable."""

    RECONNECTING = "reconnecting"
    """Port was open and has failed; the driver is retrying on its backoff schedule. Distinct
    from DISCONNECTED because a deliberate disconnect and a yanked USB cable must not look the
    same to the health watchdog."""

    FAULTED = "faulted"
    """Unrecoverable: the port cannot be opened at all, or reconnection has been abandoned."""


@dataclass(frozen=True, slots=True)
class TargetDetection:
    """One target report from a radar unit, in the unit's own frame.

    Hardware-agnostic by construction: every field here is something any short-range radar can
    be expected to produce, and anything peculiar to a particular unit is either dropped or
    written to the raw log rather than smuggled through this type.

    Angles are **relative to the sensor's own boresight**, not to true north, and not corrected
    for how the unit is mounted. Resolving them into the world needs the mount geometry, which
    is configuration the driver does not own and the adapter does.
    """

    timestamp: float
    """Monotonic seconds, from the injected `Clock`, at which the frame carrying this target
    was decoded. Monotonic rather than wall time because the only thing this layer uses it for
    is intervals and staleness, and wall time steps backwards under NTP correction (ADR 0003).
    The adapter attaches the wall-clock timestamp that leaves the system."""

    range_m: float
    """Slant range along the beam. Not ground range: at a tilted mount the two differ."""

    azimuth_deg: float
    """Degrees off boresight in the beam plane. Positive is clockwise seen from above, i.e.
    toward increasing compass bearing once the mount's own bearing is applied."""

    elevation_deg: float | None
    """Degrees above the beam plane, or **None** when the unit cannot measure elevation.

    None is not a placeholder to be filled in with a guess. A unit with a single elevation
    channel does not know where in its vertical beam a target sits, and the honest report of
    that is absence. The adapter turns absence into a large, explicit vertical uncertainty; a
    driver that invented a zero here would turn it into false precision instead (FM-004)."""

    velocity_mps: float
    """Radial velocity: rate of change of `range_m`. Positive is receding, negative is
    approaching — the same sign convention as `Kinematics.radial_velocity_mps` downstream, so
    that no conversion sits between two places that both call it range rate.

    This is a genuine measurement on a Doppler-capable unit. It is *not* speed: a target
    crossing the beam at speed shows a range rate near zero."""

    confidence: float | None
    """Source's own probability that this return is a real object, or None when the unit does
    not express one.

    None where the hardware reports only an uncalibrated signal-strength index. Rescaling such
    an index into 0..1 and calling it confidence would manufacture a number the downstream
    risk and classification stages are entitled to treat as meaningful, and nothing in the
    hardware supports it."""

    raw_source: str
    """Model designation of the unit that produced this report, for the raw log and for
    diagnostics.

    Required rather than defaulted. The specification for this subsystem gave it a default of
    the current unit's designation; that default is the one thing in the design that would have
    put a specific model's name into the shared contract, where the next unit would silently
    inherit it. Every driver states its own."""


@dataclass(frozen=True, slots=True)
class SensorStatus:
    """What the driver knows about itself. Facts only, no interpretation.

    Exists so that a dead link is distinguishable from a quiet sky. A driver that reported only
    its target list would make "the USB adapter fell out" and "nothing is flying" identical
    observations, and the operator would be shown an empty, confident display in both cases
    (FM-010).
    """

    state: RadarConnectionState
    reason: str
    """Human-readable explanation of `state`, always populated — including when the state is
    healthy. A status that only explains itself when something is wrong forces the layer above
    to invent the wording for the healthy case, and the two then drift apart."""

    port: str | None
    """Serial device the driver is using, or None when it has never opened one."""

    baud_rate: int

    last_frame_monotonic: float | None
    """Monotonic time of the last frame that decoded cleanly, or None if there has been none
    since `connect()`. This — not the target count — is what tells the watchdog the link is
    alive: a radar in an empty sky still emits frames saying it sees nothing."""

    frames_decoded: int
    """Well-formed frames since `connect()`."""

    targets_reported: int
    """Target reports extracted since `connect()`."""

    malformed_frames: int
    """Frames rejected for a bad header, length, checksum or tail. A nonzero and *growing*
    count is the signature of a wrong baud rate or a wrong protocol assumption, which is
    exactly the failure this testbed is most likely to hit first."""

    bytes_discarded: int
    """Bytes dropped while hunting for frame sync. Distinguished from `malformed_frames`
    because a steady trickle here with frames still decoding is normal resynchronisation,
    whereas a flood with no decodes means the byte stream is not the protocol we think it is."""

    reconnect_attempts: int
    """Cumulative across the session, not reset by a successful reconnect. A link that recovers
    and drops again is a flapping link — usually a marginal cable or a failing adapter — and a
    counter zeroed on every recovery would erase exactly that diagnosis."""


class RadarSensor(ABC):
    """A physical radar unit, behind a contract that outlives it.

    ## Lifecycle

        construct -> connect() -> get_targets()* -> disconnect()

    Construction is cheap and side-effect free; no port is opened until `connect()`. That is
    what lets a full set of sensors be built from configuration without the build failing
    halfway through on the one unit whose USB adapter is unplugged.

    `get_targets()` before `connect()` raises. Returning an empty list instead would disguise a
    wiring error as an empty sky, which is the failure this whole interface is shaped around.

    ## Time

    The clock is injected and is the only source of time (ADR 0003). No driver calls
    `time.monotonic()` or `datetime.now()` directly; `tests/unit/test_module_boundaries.py`
    fails the build if one does. This matters more here than it looks: a driver's reconnect
    backoff and staleness logic are pure interval arithmetic, and testing them by sleeping
    would make the suite slow and flaky at once.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open the link and prepare the unit. Idempotent — connecting a connected unit is not
        an error.

        Applies whatever configuration the unit needs to hold in its own firmware. Raises
        `RadarSensorError` if the port cannot be opened at all, because a system that starts
        with a radar it never reached must say so at startup rather than three hours in.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Close the link and release the port. Idempotent, and safe to call after a fault."""

    @abstractmethod
    def get_targets(self) -> list[TargetDetection]:
        """Every target reported since the previous call, oldest first.

        Returns an empty list when the unit reported nothing, which is the normal case for most
        calls. Empty means "the radar saw nothing", never "something went wrong" — failures
        surface through `get_status()` or as `RadarSensorError`, never as silence.

        Must not block for longer than the configured read timeout. This is polled from the
        same loop that runs the rest of the pipeline.
        """

    @abstractmethod
    def get_status(self) -> SensorStatus:
        """Current self-report. Cheap to call — polled every cycle."""

    @abstractmethod
    def configure(self, **settings: object) -> None:
        """Apply tunable settings to the unit, and persist them for use across reconnects.

        Keyword-only and loosely typed at this boundary on purpose: what is tunable differs
        genuinely between units, and a fixed positional signature here would either be the
        current unit's parameter list wearing a generic name, or the union of every unit's
        parameters with most of them inapplicable. Each driver validates the settings it
        understands and raises `RadarSensorError` on one it does not, so a typo in
        configuration fails loudly rather than being ignored.

        The settings that matter for the unit on the bench — detection sensitivity, the
        consecutive-detection threshold, and the maximum detection distance — are exposed
        rather than hardcoded because this unit ships with vehicle-scale tuning and its
        behaviour against a 135 g airframe is unverified. They are expected to be swept, not
        set once.
        """
