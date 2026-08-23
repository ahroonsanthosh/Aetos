"""Everything about this device that a deployment sets rather than a build.

Read from the `driver_config` block of the registry entry and validated here, not there: the
registry validates what every sensor has in common and leaves the driver to validate its own,
because a loader that knew every driver's parameters would need editing for every new device.

Two of these fields are not tuning and are worth reading before running anything.

**`azimuth_sign` is a calibration, not a preference.** The published protocol says the reported
angle is the byte minus 0x80. It does not say whether positive means clockwise from boresight or
anticlockwise, and getting it wrong mirrors the picture about the boresight axis — a target on
the left is drawn on the right, at exactly the right range, with no other symptom. There is no
way to determine it from the documentation, so it is configuration with a documented bench
procedure behind it (`BENCH.md`, step 6).

**`sensor_latency_ms` is how stale a frame already is when its last byte lands.** It cannot be
measured from the host and is not documented, so it is declared. The adapter subtracts it, along
with the transmission time it *can* compute, to recover observation time from arrival time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sensors.adapters.capabilities import Coverage, TimingDeclaration
from sensors.adapters.ld2451.protocol import PlausibilityEnvelope

DEFAULT_BAUD_RATE = 115200
"""What the device ships with, per the published protocol: 115200, 8N1, no parity.

The rate is persistent across power cycles and configurable up to 460800, which is why both
115200 and 256000 are reported in the wild. `baud_rate_candidates` exists because of that: the
adapter probes rather than assuming, and says which rate worked."""


class Ld2451DriverConfig(BaseModel):
    """The `driver_config` block of a live registry entry for this device."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str = Field(min_length=1, max_length=256)
    """Serial device path. `/dev/ttyUSB0` on Linux, `/dev/cu.usbserial-…` on macOS, `COM3` on
    Windows. The one field that is certainly different on your machine."""

    baud_rate: int = Field(default=DEFAULT_BAUD_RATE, ge=1200, le=921600)

    baud_rate_candidates: tuple[int, ...] = (115200, 256000)
    """Rates to try if the configured one produces no decodable frames.

    Probing rather than assuming, because the wrong rate does not fail cleanly: it produces a
    stream of plausible-looking bytes that decode to nothing, which looks exactly like a sensor
    that is powered but not detecting."""

    probe_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    """How long to listen at each candidate rate before judging it. Must comfortably exceed one
    update period, or a working rate is rejected for having said nothing yet."""

    read_timeout_s: float = Field(default=0.05, gt=0.0, le=5.0)
    """How long each read waits. Short, because it also bounds how long `stop()` takes: the
    reader thread checks whether it should still be running between reads."""

    read_chunk_bytes: int = Field(default=4096, ge=64, le=65536)

    queue_capacity: int = Field(default=256, ge=1, le=100_000)
    """Frames held between reads by the reader thread and drains by the pipeline.

    Bounded on purpose (ADR 0008). When it fills, the **oldest** frame is dropped and counted:
    on a live picture the freshest observation is the one worth keeping, and a stale detection
    would be gated out by the tracker anyway. Silent dropping is what is forbidden, not
    dropping."""

    sensor_latency_ms: float = Field(default=30.0, ge=0.0, le=5000.0)
    """Delay between the observation and the last byte of its frame leaving the device.

    Subtracted to recover observation time. Not measurable from the host and not documented, so
    it is a declaration — and `timing.timestamp_uncertainty_ms` in the registry entry is how
    wrong it is allowed to be."""

    vertical_aperture_deg: float = Field(default=30.0, gt=0.0, le=180.0)
    """Total vertical beam extent. Bounds the altitude the adapter has to assume, since the
    device has no elevation channel — see ADR 0019 and `assumed_altitude_sigma_m`."""

    range_sigma_m: float = Field(default=1.0, ge=0.0, le=100.0)
    """One-sigma range error beyond quantisation. Quantisation is added in quadrature by the
    adapter, so this is the device's accuracy, not its resolution."""

    azimuth_sigma_deg: float = Field(default=3.0, ge=0.0, le=45.0)
    """One-sigma bearing error beyond quantisation. Dominates cross-range error at any useful
    range: three degrees is 5.2 m at 100 m, against 0.3 m of range error."""

    azimuth_sign: Literal[-1, 1] = 1
    """Whether a positive reported angle means clockwise from boresight (+1) or anticlockwise
    (-1). A calibration — see this module's docstring and `BENCH.md` step 6."""

    plausibility: PlausibilityEnvelope
    """The integrity check. On a protocol with no checksum this is the only one available, and
    its tightness decides how much corruption gets through (ADR 0018)."""

    link_window_s: float = Field(default=10.0, gt=0.0, le=600.0)
    """Window over which link quality is judged. Long enough that one bad frame is not a fault,
    short enough that a degrading cable is visible before it has been lying for a minute."""

    degraded_reject_rate: float = Field(default=0.02, gt=0.0, le=1.0)
    """Windowed frame reject rate at which the sensor reports DEGRADED."""

    offline_reject_rate: float = Field(default=0.25, gt=0.0, le=1.0)
    """Rate at which it reports OFFLINE and emits nothing.

    Refusing to report is deliberate. A source that is probably corrupting data is worse than a
    source that is quiet: a quiet sensor produces coasting tracks and a visible gap, both of
    which the system handles honestly, while a lying one produces confident phantom targets
    (ADR 0018)."""

    silence_timeout_s: float = Field(default=3.0, gt=0.0, le=300.0)
    """How long the link may deliver **no bytes at all** before the sensor reports OFFLINE.

    This is a link judgement, not a coverage one, and the distinction matters. A powered device
    transmits frames continuously whether or not anything is in front of it — empty frames are
    normal and decode fine. So zero bytes means the cable, the power or the port, which the
    adapter can know; zero *detections* while bytes flow means an empty sky, which it cannot and
    must not judge (FM-010)."""

    @model_validator(mode="after")
    def _probe_covers_an_update(self) -> Ld2451DriverConfig:
        if self.probe_seconds <= self.read_timeout_s:
            raise ValueError(
                f"probe_seconds ({self.probe_seconds}) must exceed read_timeout_s "
                f"({self.read_timeout_s}), or the probe judges a baud rate before a single "
                "read has completed"
            )
        return self

    @model_validator(mode="after")
    def _degraded_before_offline(self) -> Ld2451DriverConfig:
        if self.degraded_reject_rate >= self.offline_reject_rate:
            raise ValueError(
                f"degraded_reject_rate ({self.degraded_reject_rate}) must be below "
                f"offline_reject_rate ({self.offline_reject_rate}); a link that goes straight "
                "from healthy to silent gives nobody a chance to notice it degrading"
            )
        return self

    def check_against(self, coverage: Coverage, timing: TimingDeclaration) -> None:
        """Refuse a configuration that contradicts what the registry entry declares.

        Both halves are build-time refusals rather than run-time corrections, and deliberately
        so: when the adapter would behave differently from the entry, the entry is what is
        wrong, and quietly making the behaviour fit it would hide that.

        The plausibility envelope and the declared coverage answer different questions — one
        asks whether a byte value is physically possible, the other states what the sensor is
        claimed to see — and they are allowed to differ. What they may not do is let the
        envelope be *wider* than the coverage, because then the adapter emits detections outside
        its own declaration: R0.5 fails, the conformance suite says so, and, more importantly,
        an operator reading an empty sector cannot rely on the coverage that emptiness is
        interpreted against (FM-010).

        Checked here, at build time, rather than by dropping detections at run time. Silently
        discarding real returns to stay inside a declaration is the wrong repair: the
        declaration is what is wrong.
        """
        envelope_extent = 2.0 * self.plausibility.max_azimuth_deg
        if envelope_extent > coverage.azimuth_extent_deg:
            raise ValueError(
                f"the plausibility envelope admits angles to +/-"
                f"{self.plausibility.max_azimuth_deg} deg ({envelope_extent:.0f} deg total), "
                f"wider than the {coverage.azimuth_extent_deg:.0f} deg this entry declares as "
                "its coverage. The adapter would emit detections outside its own declared "
                "coverage. Widen azimuth_extent_deg to match what you are willing to accept, or "
                "tighten max_azimuth_deg to match what you claim to see"
            )

        if self.plausibility.max_distance_m > coverage.max_range_m:
            raise ValueError(
                f"the plausibility envelope admits ranges to "
                f"{self.plausibility.max_distance_m} m, beyond the "
                f"{coverage.max_range_m:.0f} m declared as coverage"
            )

        if self.plausibility.min_distance_m < coverage.min_range_m:
            raise ValueError(
                f"the plausibility envelope admits ranges from "
                f"{self.plausibility.min_distance_m} m, inside the "
                f"{coverage.min_range_m:.0f} m declared blind zone"
            )

        # Every frame in one read is timestamped from that read's completion, so the read
        # timeout is the error bar on observation time. Longer than an update period and a
        # single read batches several frames that were seconds apart in the sky and gives them
        # all the same instant — which is systematic error, and the kind the filter cannot
        # average away.
        if self.read_timeout_s >= timing.update_period_s:
            raise ValueError(
                f"read_timeout_s ({self.read_timeout_s}) is not below the declared update "
                f"period ({timing.update_period_s:.3f} s at {timing.update_rate_hz} Hz). Reads "
                "that span more than one update batch frames together and timestamp them "
                "identically, and timing error at target speed is position error that does not "
                "average out"
            )
