"""Talk to the device directly, before any of Aetos is involved.

    python -m sensors.adapters.ld2451.bench --list
    python -m sensors.adapters.ld2451.bench --port /dev/ttyUSB0
    python -m sensors.adapters.ld2451.bench --port /dev/ttyUSB0 --raw
    python -m sensors.adapters.ld2451.bench --port /dev/ttyUSB0 --record capture.bin

This exists because the first question with new hardware is never "does the tracker work". It is
"is anything coming out of this at all, and is it what the document says". Answering that
through the full pipeline means a failure at the wire looks identical to a failure at the site
configuration, and the two have nothing to do with each other.

So this uses the decoder and the transport and **nothing else** — no site, no geometry, no
tracker, no database. What it shows is what the device said.

`--record` writes the raw byte stream to a file. That is the most valuable thing here: a capture
from real hardware can be replayed into the decoder forever afterwards, as a test fixture that
no longer needs the device. Take one early, and take another the first time something looks
wrong.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from backend.common.clock import Clock, SystemClock
from sensors.adapters.ld2451.protocol import (
    FrameDecoder,
    PlausibilityEnvelope,
    RejectReason,
    TargetFrame,
)
from sensors.adapters.ld2451.transport import SerialTransport, TransportError

CLOCK: Clock = SystemClock()
"""Even here. ADR 0003 admits no exception, and this tool has no business being the one place
that reads the wall clock directly — a rule with one exemption is a rule with two before long.
A bench driving real hardware genuinely wants real time, which is exactly what `SystemClock`
is."""

DEFAULT_ENVELOPE = PlausibilityEnvelope(
    max_azimuth_deg=25,
    min_distance_m=1,
    max_distance_m=100,
    max_speed_kmh=80,
    min_snr=0,
    max_snr=100,
    max_targets=10,
)
"""Matches the shipped registry entry, so what the bench accepts is what the adapter will."""


def list_ports() -> int:
    """Every serial device the host can see, so the port name is looked up rather than guessed.

    A wrong port name is the single most common first failure, and it produces a permission or
    not-found error that says nothing about which name would have worked.
    """
    try:
        from serial.tools import list_ports as tools  # noqa: PLC0415
    except ImportError:
        print(
            'pyserial is not installed. Install the serial extra:  pip install -e ".[serial]"',
            file=sys.stderr,
        )
        return 2

    ports = list(tools.comports())
    if not ports:
        print("No serial ports found.")
        print()
        print("If the adapter is plugged in:")
        print("  - Linux: is it in `dmesg | tail`? Are you in the dialout group?")
        print("  - macOS: look for /dev/cu.usbserial-* or /dev/cu.SLAB_USBtoUART")
        print("  - Windows: check Device Manager for a CP210x under Ports (COM & LPT)")
        return 1

    print(f"{len(ports)} serial port(s):")
    for port in ports:
        print(f"  {port.device:<24} {port.description}")
        if port.hwid and port.hwid != "n/a":
            print(f"  {'':<24} {port.hwid}")
    print()
    print("The USB-to-TTL bridge is usually the one describing itself as a CP2102 or CP210x.")
    return 0


@dataclass
class _Totals:
    """What the run has seen. Printed as a summary, and the thing worth writing down."""

    frames: int = 0
    targets: int = 0
    empty_frames: int = 0
    max_targets_in_frame: int = 0
    nearest_m: int | None = None
    furthest_m: int | None = None

    def record(self, frame: TargetFrame) -> None:
        self.frames += 1
        self.targets += len(frame.targets)
        self.max_targets_in_frame = max(self.max_targets_in_frame, len(frame.targets))
        if not frame.targets:
            self.empty_frames += 1
        for target in frame.targets:
            near = self.nearest_m
            far = self.furthest_m
            self.nearest_m = target.distance_m if near is None else min(near, target.distance_m)
            self.furthest_m = target.distance_m if far is None else max(far, target.distance_m)


def run(arguments: argparse.Namespace) -> int:
    envelope = DEFAULT_ENVELOPE
    residual = FrameDecoder(envelope).residual_corruption_probability

    print(f"Aetos bench — {arguments.port}")
    print("=" * 70)
    print(
        f"plausibility  +/-{envelope.max_azimuth_deg} deg, "
        f"{envelope.min_distance_m}-{envelope.max_distance_m} m, "
        f"<= {envelope.max_speed_kmh} km/h, <= {envelope.max_targets} targets"
    )
    print(
        f"residual      {residual:.1%} of single-byte corruptions would survive these checks "
        "(ADR 0018)"
    )
    print()

    transport, baud, decoder = _connect(arguments, envelope)
    if transport is None:
        return 1

    print(f"decoding at {baud} baud. Ctrl-C to stop.")
    print("-" * 70)

    recording = Path(arguments.record).open("wb") if arguments.record else None
    totals = _Totals()
    started = CLOCK.monotonic()

    try:
        while CLOCK.monotonic() - started < arguments.duration:
            data = transport.read(4096, 0.1)
            if not data:
                continue
            if recording is not None:
                recording.write(data)
            if arguments.raw:
                print(f"  raw  {data.hex(' ')}")

            for frame in decoder.feed(data):
                totals.record(frame)
                _print_frame(frame, arguments)
    except KeyboardInterrupt:
        print()
    except TransportError as exc:
        print(f"\nlink failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if recording is not None:
            recording.close()
        transport.close()

    _summarise(totals, decoder, residual, CLOCK.monotonic() - started, arguments)
    return 0


def _connect(arguments: argparse.Namespace, envelope: PlausibilityEnvelope):
    """Find the rate the device is actually using, by decoding at it.

    The wrong rate does not fail cleanly — it delivers bytes that frame up to nothing, which
    looks exactly like a powered sensor detecting nothing. So each candidate is judged by
    whether a frame came out of it, never by whether the port opened.
    """
    candidates = [arguments.baud, *(b for b in (115200, 256000) if b != arguments.baud)]

    for baud in candidates:
        print(f"trying {baud} baud ... ", end="", flush=True)
        transport = SerialTransport(arguments.port, baud)
        try:
            transport.open()
        except TransportError as exc:
            print("failed")
            print(f"\n{exc}", file=sys.stderr)
            return None, None, None

        decoder = FrameDecoder(envelope)
        deadline = CLOCK.monotonic() + arguments.probe
        seen = 0
        while CLOCK.monotonic() < deadline:
            data = transport.read(4096, 0.1)
            if data:
                seen += len(data)
                if decoder.feed(data):
                    print(f"ok — {decoder.statistics.frames_decoded} frame(s)")
                    return transport, baud, decoder

        if seen:
            print(f"{seen} bytes, no frames — wrong rate")
        else:
            print("silent")
        transport.close()

    print()
    print("No baud rate produced a decodable frame. In order of likelihood:", file=sys.stderr)
    print("  1. No power. The module needs 5 V, not the adapter's 3.3 V pin.", file=sys.stderr)
    print("  2. TX/RX not crossed. Module TX goes to adapter RX, and RX to TX.", file=sys.stderr)
    print("  3. Adapter logic level left at 5 V. It must be switched to 3.3 V.", file=sys.stderr)
    print("  4. No common ground between the module and the adapter.", file=sys.stderr)
    print("  5. Configured to a rate this did not try — pass --baud.", file=sys.stderr)
    return None, None, None


def _print_frame(frame: TargetFrame, arguments: argparse.Namespace) -> None:
    if not frame.targets:
        if arguments.show_empty:
            print("  (no targets)")
        return

    for target in frame.targets:
        direction = "->" if target.approaching else "<-"
        # The angle is printed with its sign as the device reported it, before any calibration
        # is applied. That is the number `azimuth_sign` is calibrated against: stand to one side
        # and see which way it goes (BENCH.md step 6).
        print(
            f"  {target.distance_m:4d} m  {target.angle_deg:+4d} deg  {direction} "
            f"{target.speed_kmh:3d} km/h  snr {target.snr:3d}"
            + ("  [alarm]" if frame.alarm else "")
        )


def _summarise(
    totals: _Totals,
    decoder: FrameDecoder,
    residual: float,
    elapsed: float,
    arguments: argparse.Namespace,
) -> None:
    statistics = decoder.statistics
    print("-" * 70)
    print(f"ran for {elapsed:.1f} s")
    print(f"  frames decoded     {totals.frames}  ({totals.frames / max(elapsed, 1e-9):.1f}/s)")
    print(f"  of which empty     {totals.empty_frames}")
    print(f"  targets seen       {totals.targets}, at most {totals.max_targets_in_frame} at once")
    if totals.nearest_m is not None:
        print(f"  range span         {totals.nearest_m}-{totals.furthest_m} m")

    print()
    print("Link integrity")
    print(f"  bytes consumed     {statistics.bytes_consumed}")
    print(f"  bytes discarded    {statistics.bytes_discarded}"
          f"  ({statistics.byte_discard_rate:.2%})")
    print(f"  frames rejected    {statistics.frames_rejected}"
          f"  ({statistics.frame_reject_rate:.2%})")
    for reason in RejectReason:
        count = statistics.rejects.get(reason, 0)
        if count:
            print(f"    {reason.value:<20} {count}")
    if statistics.command_frames_skipped:
        print(f"  config replies     {statistics.command_frames_skipped}")
    if statistics.alarm_direction_disagreements:
        print(
            f"  alarm/direction disagreements  "
            f"{statistics.alarm_direction_disagreements} — the flag's meaning is unconfirmed; "
            "a high count on a clean link means it means something other than 'approaching'"
        )

    estimated = statistics.estimated_undetected_rate(residual)
    print(
        f"  estimated undetected corruption  {estimated:.2%} of accepted frames "
        f"(reject rate x residual / (1 - residual))"
    )
    print()
    if statistics.frame_reject_rate > 0.02:
        print("  ^ that reject rate is high. Shorten the leads, check the ground, move away")
        print("    from motors and switching supplies, and try the lower baud rate.")
    else:
        print("  ^ a clean link. Record this figure; it is the baseline to compare against")
        print("    when something starts going wrong.")

    if arguments.record:
        print()
        print(f"raw stream written to {arguments.record}. Keep it: it replays into the decoder")
        print("as a test fixture that never needs the hardware again.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sensors.adapters.ld2451.bench",
        description=(
            "Talk to the radar directly. Uses the wire codec and the serial port and nothing "
            "else — no site, no tracker, no database."
        ),
    )
    parser.add_argument("--list", action="store_true", help="list serial ports and exit")
    parser.add_argument("--port", default=None, help="serial device, e.g. /dev/ttyUSB0 or COM3")
    parser.add_argument("--baud", type=int, default=115200, help="rate to try first")
    parser.add_argument("--probe", type=float, default=3.0, help="seconds to test each rate")
    parser.add_argument("--duration", type=float, default=3600.0, help="seconds to run")
    parser.add_argument("--raw", action="store_true", help="hex-dump every read")
    parser.add_argument(
        "--show-empty", action="store_true", help="print frames carrying no targets"
    )
    parser.add_argument("--record", default=None, help="write the raw byte stream to this file")
    arguments = parser.parse_args(argv)

    if arguments.list:
        return list_ports()
    if not arguments.port:
        parser.error("--port is required (or use --list to find one)")
    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())
