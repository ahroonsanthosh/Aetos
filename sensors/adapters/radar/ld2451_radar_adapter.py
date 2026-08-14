"""Placeholder for the physical radar adapter. Nothing here is implemented.

This is the single file to fill in when the unit arrives. It exists now, empty, so that the
shape of the work is fixed while the surrounding code is still soft: every method below raises
`NotImplementedError`, and the class already satisfies `SensorAdapter`, so wiring it in is a
change of which constructor the composition point calls and nothing else.

The model name in this filename is a deliberate and contained exception to the rule that no
product name appears in the codebase. A driver for a specific unit has to be named after the
unit it drives; what matters is that the name stops here. Nothing outside
`sensors/adapters/radar/` refers to this module, imports it, or knows a UART exists.

--------------------------------------------------------------------------------------------
WHAT THE IMPLEMENTATION WILL NEED
--------------------------------------------------------------------------------------------

Transport
    A UART on the host — a USB-serial bridge on a development machine, a hardware UART on a
    board — read with `pyserial` or equivalent. Baud rate, port path and any configuration
    frames sent at startup belong in `sensor_config.yaml` under a `radar.serial:` block, not
    in this file. The `pyserial` dependency is optional and imported lazily in `start()`, the
    same way the camera adapter treats its inference library, so that a build without the
    hardware still imports cleanly.

Framing
    The device emits length-delimited frames with fixed header and footer byte sequences, a
    payload, and a checksum. The reader must:

    - Resynchronise on garbage rather than desynchronise permanently. A partial read at
      startup is normal; scanning forward to the next header is the recovery, and a reader
      that instead consumes bytes forever is a class of bug that only appears in the field.
    - Validate the checksum and discard frames that fail it, counting the failures. A rising
      discard rate is a cable or grounding problem and is exactly what `health()` should
      surface as DEGRADED — silently dropping bad frames turns a failing cable into an
      apparently empty sky (FM-010).
    - Never block the poll loop. `get_detections()` returns what has arrived since the last
      call, so the serial read runs on its own thread into a bounded queue, and the queue
      drops oldest on overflow rather than growing without limit.

Per-target payload
    The unit reports, per target: angle relative to boresight, distance, radial speed with a
    direction flag (approaching versus receding), and a signal-strength figure. It reports no
    elevation, and it does not classify.

    Conversions this adapter owns, all of them non-optional (obligations 2-5 on
    `SensorAdapter`):

    - Units. Device-native units are not SI; convert on the way out, once, here.
    - Direction sign. Map the approach/recede flag onto the canonical sign convention:
      `radial_velocity_mps` is negative toward the sensor, positive away. Getting this
      backwards produces tracks that close when they are opening, which looks plausible.
    - Frame. Angle is relative to boresight; add the configured `boresight_deg` to get true
      bearing, then place the target at that bearing and distance in the site's local ENU
      frame and convert to WGS-84, exactly as `StubRadarAdapter._fabricate` does.
    - Elevation. The unit measures none. Do not invent one: use the mounting height from
      config as the target's up-coordinate and inflate the vertical term of the uncertainty
      to say plainly that the height is an assumption rather than a measurement.
    - Uncertainty. Radar error is strongly anisotropic — precise in range, imprecise in
      cross-range — so populate `position_covariance_enu`, not just the isotropic scalar.
      Collapsing it into one number makes the downstream filter confident in a direction the
      sensor is not.
    - Confidence. Derive `Quality.confidence` from the reported signal strength against the
      device's own scale, and populate `snr_db` where the figure is genuinely a signal-to-
      noise ratio. If it is not, leave `snr_db` None rather than reporting a number in the
      wrong units.
    - Time. Timestamp with when the observation was made, from the injected clock, and
      account for the frame's transit latency if it proves measurable. Never `datetime.now()`
      (ADR 0003).

Sensor type
    Report `SensorType.RADAR`, not `SIMULATED_RADAR`. That value is currently a schema
    reservation; this adapter is what makes it a capability claim.

Validation before this is trusted
    Bearing and range checked against a surveyed reference at known positions, the direction
    sign confirmed against a target moving in a known direction, and the uncertainty figures
    checked against observed scatter rather than the datasheet. Uncertainty copied from a
    datasheet is usually optimistic, and an optimistic covariance is worse for the filter than
    an honestly pessimistic one.
"""

from __future__ import annotations

from backend.common.clock import Clock
from backend.common.geodesy import LocalFrame
from sensors.adapters.interface import SensorAdapter, SensorHealth
from sensors.config.loader import RadarConfig
from sensors.schemas.detection import Detection, SensorType

_NOT_IMPLEMENTED = (
    "the physical radar adapter is not implemented; "
    "see the module docstring in sensors/adapters/radar/ld2451_radar_adapter.py"
)


class Ld2451RadarAdapter(SensorAdapter):
    """The physical radar, once it exists. Every method raises today.

    Raising rather than returning empty is the point: an unimplemented adapter that returned
    no detections would be indistinguishable from a working one watching a quiet sky, and the
    first time anyone noticed would be when something was missed.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        config: RadarConfig,
        frame: LocalFrame,
    ) -> None:
        super().__init__(clock, config.sensor_id, SensorType.RADAR)
        self._config = config
        self._frame = frame

    def start(self) -> None:
        """TODO: open the serial port, send any configuration frames, start the reader."""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def stop(self) -> None:
        """TODO: stop the reader thread and close the port. Must be idempotent."""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_detections(self) -> list[Detection]:
        """TODO: drain the frame queue, convert each target, return canonical Detections."""
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def health(self) -> SensorHealth:
        """TODO: report OK, or DEGRADED with the checksum-failure rate, or OFFLINE."""
        raise NotImplementedError(_NOT_IMPLEMENTED)
