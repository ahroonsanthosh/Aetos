"""Where the bytes come from. One narrow interface, one real implementation, one fake.

The adapter reads a `ByteTransport`, not a serial port. That is not indirection for its own
sake: it is what lets every test in `tests/unit/test_radar_adapter.py` drive the full adapter
— framing, geometry, uncertainty, health — from captured bytes with no hardware attached, on
a machine that has no serial port at all. A hardware integration that can only be tested by
plugging the hardware in is a hardware integration that stops being tested.

`pyserial` is imported inside `open()` rather than at module import. The dependency is an
optional extra, and a laptop running the simulator should not fail to import the sensors
package because a driver for hardware it does not have is missing.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


class TransportError(RuntimeError):
    """The link could not be opened, or failed while open."""


@runtime_checkable
class ByteTransport(Protocol):
    """A one-way byte source that can be opened and closed.

    Reading is expected to block for at most the transport's own timeout and to return what it
    has, including nothing. A transport that blocks indefinitely would hang the reader thread
    at shutdown, so the timeout is part of the contract rather than a setting.
    """

    def open(self) -> None:
        """Acquire the link. Raises `TransportError` if it cannot be acquired."""

    def close(self) -> None:
        """Release it. Idempotent, and safe to call on a link that never opened."""

    def read(self) -> bytes:
        """Bytes available now, up to the implementation's own limit.

        Returns `b""` when the read timed out with nothing to show, which is normal and means
        only that no bytes arrived in that window. Raises `TransportError` if the link failed.
        """


class SerialTransport:
    """A serial port, via `pyserial`.

    Holds the port object and nothing else — no framing, no protocol, no retry policy. Whether
    a silent port means a dead sensor is a judgement made from health records upstream, not
    here (FM-010).
    """

    def __init__(
        self,
        port: str,
        baud_rate: int = 115200,
        read_timeout_s: float = 0.2,
        read_size: int = 4096,
    ) -> None:
        self._port_name = port
        self._baud_rate = baud_rate
        self._read_timeout_s = read_timeout_s
        self._read_size = read_size
        self._port = None

    @property
    def port_name(self) -> str:
        return self._port_name

    def open(self) -> None:
        if self._port is not None:
            return
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on the install having the extra
            raise TransportError(
                "reading a serial sensor needs pyserial, which is an optional dependency. "
                "Install it with:  pip install -e '.[hardware]'"
            ) from exc

        try:
            self._port = serial.Serial(
                port=self._port_name,
                baudrate=self._baud_rate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=self._read_timeout_s,
            )
        except Exception as exc:  # serial.SerialException and OSError both occur here
            raise TransportError(
                f"could not open serial port {self._port_name} at {self._baud_rate} baud: {exc}"
            ) from exc

    def close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:  # pragma: no cover - closing a broken port is not a new failure
                pass

    def read(self) -> bytes:
        if self._port is None:
            raise TransportError(f"serial port {self._port_name} is not open")
        try:
            waiting = self._port.in_waiting or 1
            return self._port.read(min(max(waiting, 1), self._read_size))
        except Exception as exc:
            raise TransportError(f"read from {self._port_name} failed: {exc}") from exc


class ReplayTransport:
    """A fixed sequence of byte chunks, then silence. For tests and for replaying a capture.

    Silence rather than exhaustion is deliberate: a transport that raised at the end of its
    data would exercise a failure path that a real port does not have, and the interesting
    case for this adapter is precisely what it does when a port goes quiet.
    """

    def __init__(
        self, chunks: list[bytes], idle_delay_s: float = 0.01, chunk_delay_s: float = 0.0
    ) -> None:
        self._chunks = list(chunks)
        self._idle_delay_s = idle_delay_s
        self._chunk_delay_s = chunk_delay_s
        """Pause before each chunk, imitating a device that reports on a cadence rather than
        emptying its whole capture into the first read. A test that needs detections spread
        across pipeline cycles — which is what forming a track requires — sets this."""
        """Pause before returning empty, standing in for a real port's read timeout. Without
        it a reader thread spins at full speed the moment the data runs out, which is not how
        the transport it imitates behaves."""
        self._opened = False
        self._closed = False

    @property
    def exhausted(self) -> bool:
        return not self._chunks

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._closed = True

    def read(self) -> bytes:
        if not self._opened or self._closed:
            raise TransportError("transport is not open")
        if self._chunks:
            if self._chunk_delay_s:
                time.sleep(self._chunk_delay_s)
            return self._chunks.pop(0)
        time.sleep(self._idle_delay_s)
        return b""
