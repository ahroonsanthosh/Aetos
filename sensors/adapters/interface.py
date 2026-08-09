"""The `SensorAdapter` contract — the one interface every source of detections implements.

Together with the canonical `Detection` schema this is the narrow waist of the system: the
simulator and any future real sensor go through the same four methods and produce the same
shape, so that nothing downstream of ingestion can tell which kind of source it is reading.

The interface is deliberately small. Everything a sensor is peculiar about — protocol,
polling versus streaming, coordinate frame, altitude datum, units, clock discipline — is the
adapter's problem and stops here. That is what makes `RadarAdapter`, `EOIRAdapter`,
`RFSensorAdapter` and `AcousticAdapter` additions rather than changes: none of tracking,
fusion, classification or risk names a sensor kind or reads anything but a `Detection`.

See docs/architecture.md, docs/detection_schema.md and docs/adr/0002.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from backend.common.clock import Clock
from sensors.schemas.detection import Detection, SensorType


class SensorAdapterError(RuntimeError):
    """An adapter was used in a way its lifecycle does not permit, or failed irrecoverably.

    Raised rather than absorbed. An adapter that quietly returns nothing when it has been
    misconfigured or mis-wired looks exactly like a sensor observing an empty sky, which is
    the one thing an operator must never be shown by mistake (FM-010).
    """


class SensorState(StrEnum):
    """What the watchdog needs to know about a source, in one word.

    Deliberately about the *adapter's* own knowledge of itself. Whether a silent sensor is
    broken or merely looking at empty sky is not a question an adapter can answer — it is a
    judgement made in `backend/system_health` from the health record's timestamps and counts.
    """

    NOT_STARTED = "not_started"
    """Constructed but never started. Distinct from OFFLINE: nothing has gone wrong yet."""

    OK = "ok"
    """Running with its full configured capability."""

    DEGRADED = "degraded"
    """Running, but with reduced capability the adapter knows about and can name."""

    OFFLINE = "offline"
    """Stopped, or unable to observe at all. Produces no detections."""


class SensorHealth(BaseModel):
    """One source's self-report, as consumed by the system health watchdog.

    Facts only, no interpretation. `last_detection_time` and `detections_emitted` are what let
    the watchdog decide a sensor has gone silent; the adapter does not decide that for itself,
    because the threshold is a site-level policy and differs between a 10 Hz camera and a
    1 Hz radio-frequency sensor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensor_id: str = Field(min_length=1, max_length=64)
    sensor_type: SensorType
    state: SensorState

    last_detection_time: datetime | None = None
    """Timestamp of the most recently emitted detection, from the injected clock. None if the
    source has emitted nothing since it was started."""

    detections_emitted: int = Field(default=0, ge=0)
    """Count since `start()`. Reset by a restart, because the watchdog reasons about rate over
    a window and a count that survived a restart would describe a run that has ended."""

    reason: str = Field(min_length=1, max_length=256)
    """Human-readable explanation of `state`, always populated — including when the state is
    OK. A health record that only explains itself when something is wrong forces the operator
    interface to invent the wording for the healthy case, and the two then drift apart."""


class SensorAdapter(ABC):
    """A source of canonical Detections.

    ## What an adapter takes on

    Restating the obligations from docs/detection_schema.md, because this is the file an
    implementer of a new adapter reads:

    1. **Emit only valid canonical Detections.** Validation failures are the adapter's
       problem, not the pipeline's.
    2. **Convert to the canonical datum, frame and units** — metres above the WGS-84
       ellipsoid, WGS-84 latitude and longitude, SI. Real sensors variously report height
       above mean sea level, above ground level or barometric altitude; converting is the
       adapter's job and getting it wrong is a silent hundred-metre error (FM-004).
    3. **Timestamp with when the observation was made**, timezone-aware, from the injected
       clock — not when it was received or processed.
    4. **Report uncertainty honestly.** Understating it makes the downstream filter
       overconfident, which is worse than reporting nothing. Where the sensor's uncertainty
       is anisotropic, populate `position_covariance_enu` rather than collapsing it into a
       scalar the filter will believe.
    5. **Mark provenance truthfully** on `kinematics` and `classification`.
    6. **Never emit ground truth as though it were measurement.** For a simulated adapter
       this means degrading truth through the configured sensor model — noise, probability of
       detection, classification confusion — before emitting. Emitting clean truth is a bug,
       not a shortcut (ADR 0002).

    ## Lifecycle

        construct → start() → get_detections()* → stop()

    `get_detections()` before `start()` raises: polling an adapter nobody started is a wiring
    error, and returning an empty list would disguise it as an empty sky. After `stop()` it
    returns nothing, because stopping is a legitimate state a driver may poll through — a
    scenario can script a sensor offline mid-run.

    ## Time

    The clock is injected and is the only source of time (ADR 0003). No adapter calls
    `datetime.now()`; `tests/unit/test_module_boundaries.py` fails the build if one does.
    """

    def __init__(self, clock: Clock, sensor_id: str, sensor_type: SensorType) -> None:
        self._clock = clock
        self._sensor_id = sensor_id
        self._sensor_type = sensor_type

    @property
    def sensor_id(self) -> str:
        return self._sensor_id

    @property
    def sensor_type(self) -> SensorType:
        return self._sensor_type

    @abstractmethod
    def start(self) -> None:
        """Begin observing. Idempotent — starting a running adapter is not an error.

        Implementations that hold a connection open it here rather than in `__init__`, so
        that constructing the full adapter set from configuration cannot fail halfway through
        on a sensor that happens to be unreachable.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop observing and release anything held. Idempotent."""

    @abstractmethod
    def get_detections(self) -> list[Detection]:
        """Every detection observed since the previous call, oldest first.

        Returns an empty list when there is nothing to report, which is the normal case for
        most calls — an empty list means "nothing observed", never "something went wrong".
        Failures surface as `SensorAdapterError` or in `health()`, not as silence.

        Returns canonical `Detection` objects and nothing else. No adapter-specific side
        channel, no extra tuple element, no attribute smuggled onto the object: the moment a
        consumer can read something only one kind of source provides, the interchangeability
        the whole architecture rests on is gone.
        """

    @abstractmethod
    def health(self) -> SensorHealth:
        """Current self-report. Cheap to call — the watchdog polls it on every cycle."""
