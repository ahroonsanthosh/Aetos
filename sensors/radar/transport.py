"""The byte pipe, behind an interface, so that the driver can be tested without hardware.

A serial driver whose only implementation talks to a real port is a driver that is tested by
plugging something in, which means the protocol edge cases that matter most — a frame split
across two reads, a header appearing inside a payload, a stream of noise from a wrong baud rate
— are exercised by hoping they happen on the bench.

`SerialTransport` is therefore a three-method pipe. `PySerialTransport` is the real one;
`tests/unit/test_radar_driver.py` supplies a scripted one that hands over whatever byte sequence
the test needs. Nothing above this file knows which it has.

`pyserial` is an optional dependency (`pip install -e ".[hardware]"`). It is imported inside
`open()` rather than at module scope so that importing the radar package — which the test suite
and every non-hardware deployment do — does not require a library that only matters when a
physical unit is attached.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class SerialTransportError(RuntimeError):
    """The byte pipe could not be opened, read or written.

    One exception for every transport failure, because the driver's response to all of them is
    the same: mark the link down, start the reconnect backoff, and keep saying so in
    `get_status()` until it recovers.
    """


@runtime_checkable
class SerialTransport(Protocol):
    """A bidirectional byte pipe with a read timeout.

    Structural rather than inherited: a test double should not have to import anything from
    here to be usable, and a future transport that is not a serial port at all — a TCP bridge
    to a remote unit, a replay of a raw log — satisfies the same three methods.
    """

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None:
        """Open the pipe. Raises `SerialTransportError` if it cannot be opened."""

    def close(self) -> None:
        """Close the pipe. Idempotent, and must not raise on an already-closed pipe."""

    def read(self, max_bytes: int) -> bytes:
        """Read up to `max_bytes`, returning as soon as the read timeout expires.

        Returns `b""` when the timeout expired with nothing available, which is the normal case
        for a radar between frames. Empty is never an error and never means end-of-stream — a
        serial port has no end. Raises `SerialTransportError` when the pipe itself has failed.
        """

    def write(self, data: bytes) -> int:
        """Write `data`, returning the number of bytes accepted."""


class PySerialTransport:
    """The real serial port, via `pyserial`.

    Thin on purpose. Everything interesting — framing, retries, backoff — is the driver's, so
    that swapping the transport cannot change protocol behaviour and swapping the driver cannot
    change how the port is opened.
    """

    __slots__ = ("_port", "_baud_rate", "_timeout_s", "_serial")

    def __init__(self, port: str, baud_rate: int, read_timeout_s: float) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._timeout_s = read_timeout_s
        self._serial: object | None = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and bool(getattr(self._serial, "is_open", False))

    @property
    def baud_rate(self) -> int:
        return self._baud_rate

    def open(self) -> None:
        if self.is_open:
            return
        try:
            import serial  # noqa: PLC0415 - optional dependency, imported only when used
        except ImportError as exc:  # pragma: no cover - depends on the install extras
            raise SerialTransportError(
                "pyserial is not installed; the hardware radar driver needs it. Install the "
                'hardware extra with: pip install -e ".[hardware]"'
            ) from exc

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                timeout=self._timeout_s,
                write_timeout=self._timeout_s,
            )
        except Exception as exc:  # pyserial raises SerialException and OSError alike
            self._serial = None
            raise SerialTransportError(f"could not open {self._port}: {exc}") from exc

    def close(self) -> None:
        serial_port = self._serial
        self._serial = None
        if serial_port is None:
            return
        try:
            serial_port.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - closing a broken port must not mask the real fault
            # A port that fails to close is already gone. Raising here would replace whatever
            # fault triggered the close with a less informative one.
            return

    def read(self, max_bytes: int) -> bytes:
        if self._serial is None:
            raise SerialTransportError(f"{self._port} is not open")
        try:
            waiting = int(getattr(self._serial, "in_waiting", 0) or 0)
            # Read what is buffered, but always ask for at least one byte so the call blocks
            # for the configured timeout rather than spinning when the buffer is empty.
            return bytes(self._serial.read(min(max(waiting, 1), max_bytes)))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - any read failure is a link failure
            raise SerialTransportError(f"read from {self._port} failed: {exc}") from exc

    def write(self, data: bytes) -> int:
        if self._serial is None:
            raise SerialTransportError(f"{self._port} is not open")
        try:
            return int(self._serial.write(data) or 0)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - any write failure is a link failure
            raise SerialTransportError(f"write to {self._port} failed: {exc}") from exc
