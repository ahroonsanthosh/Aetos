"""Bring-up tool: find the baud rate, watch the frames, capture the raw stream.

    python -m sensors.adapters.uart_fmcw_radar.bringup --list-ports
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
from sensors.adapters.uart_fmcw_radar.link import (
    ByteLink,
    LinkError,
    SerialByteLink,
    SerialLibraryMissing,
    available_ports,
)
from sensors.adapters.uart_fmcw_radar.protocol import ReportParser

PROBE_SECONDS = 4.0
"""Long enough for several report periods at any plausible update rate."""

_NO_PORTS_AT_ALL = """This machine reports no serial ports at all — not the radar's, not any other.

That is a driver or cable problem on this computer, and nothing in Aetos can affect it:
  - unplug and replug the USB-to-TTL adapter, and watch whether the list changes at all,
  - on Windows, look in Device Manager for a device with a warning triangle. A CP2102 with no
    driver appears under "Other devices" rather than under "Ports (COM & LPT)",
  - the CP2102 driver is a separate install on some Windows builds — Silicon Labs publish it
    as "CP210x USB to UART Bridge VCP Drivers",
  - try another USB cable. Charge-only cables carry power and no data lines, and look
    identical to the ones that work."""


class PortUnavailable(RuntimeError):
    """The serial port could not be opened. Distinct from the sensor being silent.

    Worth its own type because the two failures have nothing in common. A port that will not
    open is a wrong device name, a missing driver, or something else already holding it — none
    of which is fixed by checking a jumper wire, and all of which are answered by looking at
    what ports the machine actually has.
    """


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
            # Not a probe result. The port could not be opened at all, which no baud rate can
            # fix — reporting it as "nothing framed" would send someone to check jumper wires
            # over a port that does not exist.
            print("cannot open")
            raise PortUnavailable(str(exc)) from exc

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
    parser.add_argument(
        "--port", help="serial device, e.g. /dev/ttyUSB0 or COM4. Required unless --list-ports."
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="list the serial ports this machine reports, and exit",
    )
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

    if arguments.list_ports:
        try:
            ports = available_ports()
        except SerialLibraryMissing as exc:
            print(exc, file=sys.stderr)
            return 1
        if ports:
            print("\n".join(ports))
            return 0
        print(_NO_PORTS_AT_ALL, file=sys.stderr)
        return 1

    if not arguments.port:
        parser.error("--port is required unless --list-ports is given")

    baud = arguments.baud
    if baud is None:
        print(f"Probing {arguments.port} for a rate that frames:")
        try:
            baud = probe(
                arguments.port, UartLinkConfig(port=arguments.port).probe_baud_rates, clock
            )
        except PortUnavailable as exc:
            return _report_missing_port(arguments.port, str(exc))
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


def _report_missing_port(port: str, reason: str) -> int:
    """Say which port failed, and show what the machine actually has.

    The list is the whole point. "COM4 not found" leaves someone guessing; "COM4 not found, but
    here is COM7 with a CP2102 on it" ends the problem in one line.
    """
    print(f"\nCould not open {port}: {reason}", file=sys.stderr)

    try:
        ports = available_ports()
    except SerialLibraryMissing as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    if ports:
        print("\nSerial ports this machine reports:", file=sys.stderr)
        for entry in ports:
            print(f"  {entry}", file=sys.stderr)
        print(
            "\nRun again with --port set to whichever of those is the radar's USB adapter "
            "(it will usually name CP2102, CH340 or FTDI).",
            file=sys.stderr,
        )
    else:
        print(f"\n{_NO_PORTS_AT_ALL}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
