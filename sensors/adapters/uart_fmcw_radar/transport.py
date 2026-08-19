"""Serial transport for the UART FMCW radar adapter.

The only file in this package that touches the operating system or a physical port. Isolating
I/O here means `protocol.py` stays a pure function library — testable without hardware or
`pyserial` installed — and it means a future hardware swap that changes the physical link
(different port, different bridge chip, a mock for tests) never touches frame parsing.

`pyserial` is imported lazily inside `open()`, not at module import time, so that importing
this package — and running `protocol.py`'s tests — never requires it to be installed. It is
only needed when an adapter actually opens a real port.
"""

from __future__ import annotations


class SerialTransportError(RuntimeError):
    """The serial link could not be opened, or a read failed irrecoverably."""


class SerialTransport:
    """Thin wrapper over `pyserial`. Non-blocking-in-spirit reads only.

    `read_available()` never waits for bytes that have not arrived yet — it returns whatever is
    already buffered by the OS, which is what lets `UartFmcwRadarAdapter.get_detections()`
    return promptly on every poll rather than blocking the pipeline's cycle on a quiet sensor.
    """

    def __init__(self, port: str, baud_rate: int, read_timeout_s: float) -> None:
        self._port_name = port
        self._baud_rate = baud_rate
        self._read_timeout_s = read_timeout_s
        self._serial_module = None
        self._handle = None

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    def open(self) -> None:
        if self._handle is not None:
            return
        if self._serial_module is None:
            try:
                import serial
            except ImportError as exc:
                raise SerialTransportError(
                    "pyserial is required for the UART FMCW radar adapter; install it with "
                    "`pip install pyserial` or `pip install .[hardware]`"
                ) from exc
            self._serial_module = serial

        try:
            self._handle = self._serial_module.Serial(
                port=self._port_name,
                baudrate=self._baud_rate,
                timeout=self._read_timeout_s,
            )
        except self._serial_module.SerialException as exc:
            raise SerialTransportError(
                f"could not open serial port {self._port_name!r}: {exc}"
            ) from exc

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def read_available(self) -> bytes:
        """Every byte currently waiting on the port. Empty bytes means nothing is waiting."""
        if self._handle is None:
            raise SerialTransportError("read_available() called before open()")
        try:
            waiting = self._handle.in_waiting
            return self._handle.read(waiting) if waiting else b""
        except self._serial_module.SerialException as exc:
            raise SerialTransportError(f"serial read failed on {self._port_name!r}: {exc}") from exc
