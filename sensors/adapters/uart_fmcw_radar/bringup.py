"""Bring-up tool: find the baud rate, watch the frames, capture the raw stream.

    python -m sensors.adapters.uart_fmcw_radar.bringup --port /dev/ttyUSB0
    python -m sensors.adapters.uart_fmcw_radar.bringup --port COM4 --baud 256000 --watch
    python -m sensors.adapters.uart_fmcw_radar.bringup --port /dev/ttyUSB0 --record var/raw.log

Run this **before** running Aetos in live mode, and run it again whenever the display goes
quiet. It answers the one question the operator interface deliberately cannot: is this sensor
saying anything at all, and is what it says being framed correctly?

Three modes, in the order you need them:

- **probe** (default) — try each candidate baud rate in turn and report which one frames. The
  wrong rate delivers a byte stream that never syncs, which downstream looks identical to a
  working sensor watching an empty sky (FM-050). This is how you tell the difference in ten
  seconds instead of an afternoon.
- **watch** — decode continuously and print each target as the sensor described it: angle,
  range, direction, speed, signal quality. Sensor-relative, unconverted, no site involved. Walk
  in front of it and watch the numbers move.
- **record** — append the raw byte stream to a file, timestamped. The dataset every claim about
  this hardware's detection performance has to be built on.

Nothing here writes to the sensor, so it cannot disturb a configured unit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.common.clock import Clock, SystemClock
from sensors.adapters.uart_fmcw_radar.config import UartLinkConfig
from sensors.adapters.uart_fmcw_radar.link import ByteLink, LinkError, SerialByteLink
from sensors.adapters.uart_fmcw_radar.protocol import ReportParser

PROBE_SECONDS = 4.0
"""Long enough for several report periods at any plausible update rate."""


def probe(
    port: str,
    rates: tuple[int, ...],
    clock: Clock,
    seconds: float = PROBE_SECONDS,
    link_factory=SerialByteLink,
) -> int | None:
    """Try each rate; return the first that produces a valid frame, or None.

    A rate is accepted only on a **decoded frame**, never on bytes arriving. Noise arrives at
    every rate — that is the whole problem — and accepting a rate because something showed up
    would confirm exactly the mistake this exists to catch.
    """
    for rate in rates:
        config = UartLinkConfig(port=port, baud=rate)
        link = link_factory(config)
        parser = ReportParser()
        print(f"  {rate:>7} baud … ", end="", flush=True)

        try:
            link.open()
        except LinkError as exc:
            print(f"cannot open: {exc}")
            return None

        try:
            _read_until(link, parser, clock, seconds, config.max_read_bytes)
        finally:
            link.close()

        stats = parser.stats
        if stats.frames_decoded:
            print(
                f"{stats.frames_decoded} frames decoded, "
                f"{stats.frames_malformed} malformed, {stats.bytes_discarded} bytes discarded"
            )
            return rate

        if stats.bytes_discarded:
            print(f"bytes arriving but nothing framed ({stats.bytes_discarded} discarded)")
        else:
            print("silent — no bytes at all")

    return None


def _read_until(
    link: ByteLink, parser: ReportParser, clock: Clock, seconds: float, chunk: int
) -> None:
    """Feed the parser until the deadline. Reads block on the port's own timeout, so this
    neither spins nor sleeps."""
    deadline = clock.monotonic() + seconds
    while clock.monotonic() < deadline:
        try:
            data = link.read(chunk)
        except LinkError as exc:
            print(f"link lost: {exc}")
            return
        if data:
            parser.feed(data)


def watch(port: str, baud: int, clock: Clock, record: Path | None = None) -> int:
    """Decode continuously, printing every target until interrupted."""
    config = UartLinkConfig(port=port, baud=baud)
    link = SerialByteLink(config)
    parser = ReportParser()

    try:
        link.open()
    except LinkError as exc:
        print(f"cannot open {port}: {exc}", file=sys.stderr)
        return 1

    if record is not None:
        record.parent.mkdir(parents=True, exist_ok=True)
    print(f"Reading {port} at {baud} baud. Ctrl-C to stop.\n")
    print(f"{'time':>12}  {'angle':>7}  {'range':>7}  {'speed':>9}  {'quality':>7}")

    frames = 0
    try:
        while True:
            try:
                data = link.read(config.max_read_bytes)
            except LinkError as exc:
                print(f"\nlink lost: {exc}", file=sys.stderr)
                return 1
            if not data:
                continue

            now = clock.now()
            if record is not None:
                with record.open("a", encoding="ascii") as handle:
                    handle.write(f"{now.isoformat()} {data.hex()}\n")

            for report in parser.feed(data):
                frames += 1
                stamp = now.strftime("%H:%M:%S.%f")[:12]
                if not report.targets:
                    print(f"{stamp:>12}  (no targets)")
                for target in report.targets:
                    direction = "closing" if target.approaching else "opening"
                    print(
                        f"{stamp:>12}  {target.azimuth_deg:>6.0f}°  "
                        f"{target.range_m:>6.0f}m  "
                        f"{target.radial_velocity_mps:>6.1f}m/s  {target.quality:>7}  "
                        f"{direction}"
                    )
    except KeyboardInterrupt:
        stats = parser.stats
        print(
            f"\n{frames} frames, {stats.frames_malformed} malformed, "
            f"{stats.bytes_discarded} bytes discarded."
        )
        if record is not None:
            print(f"Raw stream appended to {record}.")
        return 0
    finally:
        link.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bring up the serial-attached FMCW radar before running Aetos live.",
    )
    parser.add_argument("--port", required=True, help="serial device, e.g. /dev/ttyUSB0 or COM4")
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="skip the probe and use this rate. Implies --watch.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="after probing, decode continuously and print every target",
    )
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        help="append the raw byte stream to this file while watching",
    )
    arguments = parser.parse_args(argv)
    clock = SystemClock()

    baud = arguments.baud
    if baud is None:
        print(f"Probing {arguments.port} for a rate that frames:")
        baud = probe(arguments.port, UartLinkConfig(port=arguments.port).probe_baud_rates, clock)
        if baud is None:
            print(
                "\nNo candidate rate produced a decoded frame. Before assuming the sensor is "
                "dead,"
                "\n  - check the USB-to-TTL adapter's logic-level switch is on 3.3V,"
                "\n  - check TX goes to RX and RX to TX, not TX to TX,"
                "\n  - check the sensor has 5V on VCC and a common ground,"
                "\n  - check nothing else already holds the port open.",
                file=sys.stderr,
            )
            return 1
        print(f"\nFrames decode at {baud} baud. Put that in the site's hardware block.\n")

    if arguments.watch or arguments.baud is not None:
        return watch(arguments.port, baud, clock, arguments.record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
