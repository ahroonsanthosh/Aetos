"""Bench tool: read a radar module's serial port and print what it is saying.

Run this before running Aetos against the hardware, and run it again whenever the picture
looks wrong. It answers the questions the pipeline cannot:

* Is anything arriving on this port at all, and at this baud rate?
* Which direction code does *this* unit use for a target coming towards it?
* Which way does its azimuth sign run?
* At what range does it stop seeing the thing you are actually trying to detect?

Nothing here touches the pipeline, the event log or the database. It opens the port, decodes
frames and prints them, which is deliberately the smallest thing that can distinguish a wiring
fault from a sensor fault.

    python -m scripts.radar_probe --port /dev/ttyUSB0
    python -m scripts.radar_probe --port /dev/ttyUSB0 --raw        # hex, before decoding
    python -m scripts.radar_probe --port COM3 --seconds 30

The bench procedure that uses it is in docs/hardware_radar_bench.md.
"""

from __future__ import annotations

import argparse

from backend.common.clock import SystemClock
from sensors.adapters.radar.protocol import (
    FrameDecoder,
    FrameKind,
    ProtocolError,
    decode_target_frame,
)
from sensors.adapters.radar.transport import SerialTransport, TransportError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", required=True, help="serial device, e.g. /dev/ttyUSB0 or COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--approaching-code",
        type=int,
        choices=(0, 1),
        default=1,
        help="direction byte value meaning 'closing on the sensor'; the thing to determine here",
    )
    parser.add_argument(
        "--seconds", type=float, default=0.0, help="stop after this long; 0 runs until Ctrl-C"
    )
    parser.add_argument(
        "--raw", action="store_true", help="print every byte as hex as well as the decoded frames"
    )
    arguments = parser.parse_args()

    clock = SystemClock()
    transport = SerialTransport(arguments.port, baud_rate=arguments.baud, read_timeout_s=0.2)
    decoder = FrameDecoder()

    try:
        transport.open()
    except TransportError as exc:
        raise SystemExit(f"\n  {exc}\n") from exc

    print(f"Reading {arguments.port} at {arguments.baud} baud. Ctrl-C to stop.\n")
    started = clock.monotonic()
    empty_reports = 0

    try:
        while arguments.seconds <= 0.0 or clock.monotonic() - started < arguments.seconds:
            try:
                data = transport.read()
            except TransportError as exc:
                print(f"link failed: {exc}")
                break

            if not data:
                continue
            if arguments.raw:
                print(f"  raw {data.hex(' ')}")

            for frame in decoder.feed(data):
                if frame.kind is not FrameKind.TARGET_DATA:
                    print(f"  command reply: {frame.payload.hex(' ')}")
                    continue

                try:
                    decoded = decode_target_frame(
                        frame.payload, approaching_code=arguments.approaching_code
                    )
                except ProtocolError as exc:
                    print(f"  malformed target frame: {exc}")
                    continue

                if not decoded.targets:
                    empty_reports += 1
                    # Empty reports are the normal state of a working sensor looking at
                    # nothing, and printing every one buries the interesting lines.
                    if empty_reports % 20 == 0:
                        print(f"  ({empty_reports} empty reports — the link is alive)")
                    continue

                empty_reports = 0
                stamp = clock.now().strftime("%H:%M:%S")
                for target in decoded.targets:
                    print(
                        f"  {stamp}  azimuth {target.azimuth_deg:+6.1f}deg  "
                        f"range {target.range_m:5.0f}m  "
                        f"{'receding' if target.receding else 'approaching':>11}  "
                        f"{abs(target.radial_velocity_mps):5.1f} m/s  "
                        f"signal {target.signal_level:3d}"
                    )
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()

    stats = decoder.stats
    print(
        f"\n{stats.frames_decoded} frames decoded, {stats.bytes_discarded} bytes discarded "
        f"resynchronising, {stats.malformed_frames} malformed."
    )
    if stats.frames_decoded == 0:
        print(
            "\nNo frames at all. In order of likelihood: TX/RX swapped, the adapter's logic "
            "level jumper is on 5V rather than 3.3V, the module has no 5V supply, or the baud "
            "rate is not the one this unit is configured for."
        )
    elif stats.bytes_discarded > stats.frames_decoded * 8:
        print(
            "\nDiscarding far more than it decodes, which usually means the baud rate is close "
            "but wrong, or the ground line is not shared."
        )


if __name__ == "__main__":
    main()
