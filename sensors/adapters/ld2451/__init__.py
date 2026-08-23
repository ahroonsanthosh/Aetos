"""Driver package for one 24 GHz FMCW ranging module.

**This directory is the containment boundary for this device.** Everything Aetos knows about its
wire format, its quirks and its model number lives here and nowhere else —
`tests/unit/test_module_boundaries.py` fails the build if the name appears anywhere else in
`backend/` or `sensors/`. That is the property that makes the hardware replaceable: nothing
downstream of `SensorAdapter` can name it, so swapping it costs this directory and no more.

Three layers, each testable without the one below it:

| Module | Knows about |
|---|---|
| `protocol.py` | The wire format. No I/O at all |
| `transport.py` | The port. No idea what the bytes mean |
| `adapter.py` | Time, geometry and trust. Composes the other two |

`python -m sensors.adapters.ld2451.bench` drives all three against real hardware. `BENCH.md` is
the procedure for doing that from an unopened box to a measured coverage figure.
"""

from sensors.adapters.ld2451.adapter import Ld2451Adapter, build_ld2451_adapter
from sensors.adapters.ld2451.config import DEFAULT_BAUD_RATE, Ld2451DriverConfig
from sensors.adapters.ld2451.link import LinkQuality, LinkQualityWindow
from sensors.adapters.ld2451.protocol import (
    DecoderStatistics,
    FrameDecoder,
    PlausibilityEnvelope,
    ProtocolError,
    RejectReason,
    TargetFrame,
    TargetReport,
    assumed_altitude_sigma_m,
    encode_target_frame,
    residual_corruption_probability,
)
from sensors.adapters.ld2451.transport import (
    FakeTransport,
    SerialTransport,
    Transport,
    TransportError,
)

__all__ = [
    "DEFAULT_BAUD_RATE",
    "DecoderStatistics",
    "FakeTransport",
    "FrameDecoder",
    "Ld2451Adapter",
    "Ld2451DriverConfig",
    "LinkQuality",
    "LinkQualityWindow",
    "PlausibilityEnvelope",
    "ProtocolError",
    "RejectReason",
    "SerialTransport",
    "TargetFrame",
    "TargetReport",
    "Transport",
    "TransportError",
    "assumed_altitude_sigma_m",
    "build_ld2451_adapter",
    "encode_target_frame",
    "residual_corruption_probability",
]
