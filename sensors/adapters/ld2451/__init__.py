"""Driver package for one 24 GHz FMCW ranging module.

**This directory is the containment boundary for this device.** Everything Aetos knows about its
wire format, its quirks and its model number lives here and nowhere else —
`tests/unit/test_module_boundaries.py` fails the build if the name appears anywhere else in
`backend/` or `sensors/`. That is the property that makes the hardware replaceable: nothing
downstream of `SensorAdapter` can name it, so swapping it costs this directory and no more.

Currently the wire codec only. The adapter that turns its target reports into canonical
detections needs a serial transport and a live unit to be validated against, and is deliberately
not written ahead of them — see `README.md`.
"""

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

__all__ = [
    "DecoderStatistics",
    "FrameDecoder",
    "PlausibilityEnvelope",
    "ProtocolError",
    "RejectReason",
    "TargetFrame",
    "TargetReport",
    "assumed_altitude_sigma_m",
    "encode_target_frame",
    "residual_corruption_probability",
]
