"""The wire, behind a seam thin enough to fake.

`protocol.py` turns bytes into frames and knows nothing about where bytes come from. This
module gets the bytes, and knows nothing about what they mean. Keeping the two apart is what
lets the decoder be tested exhaustively with no hardware, and the adapter be tested against a
scripted link with no hardware either.

The seam is four methods. Everything a serial port is peculiar about — the driver, the latency
timer, the fact that a read returns whatever happens to be in the buffer rather than a frame —
stops here.

## Why `read` never blocks indefinitely

A read that can block forever turns a stop request into a hang, and the adapter's reader thread
would then outlive the process that asked it to stop. Every implementation here takes a timeout
and returns whatever it has, including nothing. An empty read is not an error and is the normal
case on a quiet link: the caller distinguishes "no bytes yet" from "the link is down" by how
long the nothing has been going on, which is a policy the adapter owns rather than the
transport.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from typing import Protocol, runtime_checkable


class TransportError(RuntimeError):
    """The link could not be opened, or failed irrecoverably while open.

    Distinct from an empty read. A port that is not there, a permission denied, a cable pulled
    mid-run: all conditions where continuing to read would produce silence indistinguishable
    from a quiet sensor, which is the one thing that must never happen by accident (FM-010).
    """


@runtime_checkable
class Transport(Protocol):
    """Somewhere bytes come from.

    A `Protocol` rather than a base class so that a test double is any object with the right
    shape, and so nothing here constrains how a future transport — TCP, a replayed capture, a
    microcontroller bridge — is built.
    """

    @property
    def description(self) -> str:
        """What this link is, for a health record a human reads. `/dev/ttyUSB0 @ 115200`."""
        ...

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None:
        """Acquire the link. Idempotent. Raises `TransportError` if it cannot be acquired."""
        ...

    def close(self) -> None:
        """Release it. Idempotent, and safe to call on a link that never opened."""
        ...

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """Whatever has arrived, up to `max_bytes`, waiting at most `timeout_s`.

        Returns `b""` when nothing arrived, which is normal rather than exceptional. Raises
        `TransportError` only when the link itself has failed.
        """
        ...


class SerialTransport:
    """A 3.3 V TTL UART reached through a USB bridge.

    `pyserial` is imported inside `open` rather than at module import. The import is the only
    thing in this package that needs a dependency beyond the core install, and paying for it at
    import time would mean the wire codec — which needs nothing — could not be imported on a
    machine without it. The error, when it comes, names the fix.
    """

    def __init__(self, port: str, baud_rate: int) -> None:
        self._port = port
        self._baud_rate = baud_rate
        self._handle = None

    @property
    def description(self) -> str:
        return f"{self._port} @ {self._baud_rate} 8N1"

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    @property
    def baud_rate(self) -> int:
        return self._baud_rate

    def open(self) -> None:
        if self._handle is not None:
            return

        try:
            import serial  # noqa: PLC0415 — deliberately lazy; see the class docstring
        except ImportError as exc:  # pragma: no cover — depends on the install
            raise TransportError(
                "pyserial is not installed, so no serial sensor can be reached. Install the "
                'serial extra:  pip install -e ".[serial]"'
            ) from exc

        try:
            # 8N1 with no flow control: what the device speaks, and not negotiable. The read
            # timeout is passed per call rather than set here, because the probe and the run
            # loop want different ones.
            self._handle = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.0,
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
        except Exception as exc:  # noqa: BLE001 — pyserial raises several unrelated types
            raise TransportError(
                f"could not open {self._port} at {self._baud_rate} baud: {exc}. Check the "
                "device is plugged in, that the port name is right, and that this user is "
                "permitted to open it (on Linux that usually means membership of the dialout "
                "group)."
            ) from exc

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001, S110 — closing a broken port is not a failure
                pass

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        if self._handle is None:
            raise TransportError(f"{self._port} is not open")

        try:
            self._handle.timeout = timeout_s
            return self._handle.read(max_bytes) or b""
        except Exception as exc:  # noqa: BLE001 — pyserial raises several unrelated types
            raise TransportError(f"read from {self._port} failed: {exc}") from exc


class FakeTransport:
    """A scripted link, for tests and for replaying a capture taken from a real unit.

    Exists so that every behaviour that depends on *how bytes arrive* — a frame split across
    reads, a burst, a silent link, a link that dies mid-run — is a test rather than something
    discovered on a bench. Those are exactly the behaviours a real port makes hard to provoke
    deliberately and easy to hit by accident.
    """

    def __init__(
        self,
        chunks: Iterable[bytes] = (),
        *,
        description: str = "fake",
        fail_after: int | None = None,
        pace_s: float = 0.0,
    ) -> None:
        self._chunks: Iterator[bytes] = iter(list(chunks))
        self._description = description
        self._fail_after = fail_after
        self._pace_s = pace_s
        self._reads = 0
        self._open = False
        self.open_count = 0
        self.close_count = 0

    @property
    def description(self) -> str:
        return self._description

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        if self._open:
            return
        self._open = True
        self.open_count += 1

    def close(self) -> None:
        if self._open:
            self.close_count += 1
        self._open = False

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        if not self._open:
            raise TransportError("fake transport is not open")

        self._reads += 1
        if self._fail_after is not None and self._reads > self._fail_after:
            raise TransportError("scripted link failure")

        if self._pace_s > 0.0:
            # A real port blocks: the reader thread spends almost all its time waiting, and
            # frames arrive at the sensor's update rate. Without this the fake hands over its
            # whole script in microseconds, so every frame lands in one batch with nearly
            # identical timestamps — which is not a link any deployment will ever see, and it
            # hides exactly the pacing-dependent behaviour worth testing.
            time.sleep(min(self._pace_s, timeout_s))

        # Exhausted means a quiet link, not a broken one — the same thing a real port reports
        # when the sensor has nothing to say, and the state the silence timeout is judged on.
        chunk = next(self._chunks, b"")
        return chunk[:max_bytes]
