"""The bring-up probe: what it accepts, and how it distinguishes its two failures.

The distinction under test is the useful one. "The port will not open" and "the sensor is
silent" look similar on a terminal and have nothing in common as problems — one is a wrong
device name or a missing driver, the other is wiring or a dead unit. Reporting the first as
the second sends someone to check jumper wires over a port that does not exist, which is
exactly what this tool exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.common.clock import SimClock
from sensors.adapters.uart_fmcw_radar.bringup import (
    PortUnavailable,
    SerialLibraryMissing,
    available_ports,
    probe,
)
from sensors.adapters.uart_fmcw_radar.config import UartLinkConfig
from sensors.adapters.uart_fmcw_radar.link import LinkError
from sensors.adapters.uart_fmcw_radar.protocol import REPORT_HEADER, REPORT_TAIL

START = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
FRAME = REPORT_HEADER + (6).to_bytes(2, "little") + bytes((1, 0x80, 50, 1, 36, 90)) + REPORT_TAIL


class ScriptedLink:
    """A link whose behaviour depends on the baud rate it was opened at, like a real one."""

    def __init__(self, config: UartLinkConfig, *, framing_at: int | None, noise: bool = True):
        self._baud = config.baud
        self._framing_at = framing_at
        self._noise = noise
        self.is_open = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read(self, max_bytes: int) -> bytes:
        if self._baud == self._framing_at:
            return FRAME
        return bytes(range(64)) if self._noise else b""

    def write(self, data: bytes) -> None:  # pragma: no cover - the probe never writes
        raise AssertionError("the probe must not write to the sensor")


class UnopenableLink(ScriptedLink):
    def open(self) -> None:
        raise LinkError("no such device")


def _clock() -> SimClock:
    """A clock that advances itself, so `_read_until` terminates without sleeping."""

    class SelfAdvancing(SimClock):
        def monotonic(self) -> float:
            self.advance(0.5)
            return super().monotonic()

    return SelfAdvancing(START)


def test_the_probe_reports_the_rate_that_frames() -> None:
    def factory(config: UartLinkConfig) -> ScriptedLink:
        return ScriptedLink(config, framing_at=256000)

    assert probe("COM4", (115200, 256000), _clock(), link_factory=factory) == 256000


def test_noise_at_every_rate_is_not_a_result() -> None:
    """A rate is accepted on a decoded frame, never on bytes arriving. Noise arrives at every
    rate — that is the whole problem this tool exists for (FM-050)."""

    def factory(config: UartLinkConfig) -> ScriptedLink:
        return ScriptedLink(config, framing_at=None)

    assert probe("COM4", (115200, 256000), _clock(), link_factory=factory) is None


def test_a_silent_link_is_not_a_result_either() -> None:
    def factory(config: UartLinkConfig) -> ScriptedLink:
        return ScriptedLink(config, framing_at=None, noise=False)

    assert probe("COM4", (115200, 256000), _clock(), link_factory=factory) is None


def test_no_ports_and_no_pyserial_are_different_answers(monkeypatch) -> None:
    """An empty port list must mean exactly one thing: this machine has no serial ports.

    Whoever reads it is about to reinstall a USB driver on the strength of it, and "the library
    is missing" would send them to do that for nothing. Two causes behind one silent answer is
    the failure this codebase is written against — a diagnostic tool gets no exemption.
    """
    import builtins

    real_import = builtins.__import__

    def without_pyserial(name, *args, **kwargs):
        if name.startswith("serial"):
            raise ImportError("no module named serial")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pyserial)

    with pytest.raises(SerialLibraryMissing, match="hardware extra"):
        available_ports()


def test_a_port_that_will_not_open_raises_rather_than_reporting_no_frames() -> None:
    """The failure the user actually hit: a COM port that does not exist on the machine.

    No baud rate fixes it, so it must not be reported as a probe result — it is answered by
    looking at what ports the machine has, which is what the caller does with this.
    """

    def factory(config: UartLinkConfig) -> UnopenableLink:
        return UnopenableLink(config, framing_at=None)

    with pytest.raises(PortUnavailable, match="no such device"):
        probe("COM4", (115200, 256000), _clock(), link_factory=factory)
