"""The byte link to the sensor, and the one place `pyserial` is imported.

`ByteLink` is a four-method protocol — open, close, read, write. The adapter is written
against it and never against a serial port, which buys three things:

1. **Every adapter test runs with no hardware and no `pyserial` installed.** A fake link
   replays a captured byte log, so framing, geometry and health logic are all testable in CI.
2. **The recorded byte stream is a first-class input.** Replaying a session log through the
   real parser is the only way to check the driver against what the sensor actually said, as
   opposed to what we assumed it says.
3. **`pyserial` stays an optional dependency.** It is imported inside `SerialByteLink.open`,
   so a checkout without the `hardware` extra installs, imports and tests cleanly, and the
   failure — if someone configures a real link without the extra — names the missing package
   and the extra that provides it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sensors.adapters.uart_fmcw_radar.config import UartLinkConfig


class LinkError(RuntimeError):
    """The byte link could not be opened, read or written."""


class SerialLibraryMissing(RuntimeError):
    """pyserial is not installed, so nothing can be said about this machine's ports."""


def available_ports() -> list[str]:
    """Every serial port this machine reports, described.

    Raises rather than returning nothing when pyserial is absent. An empty list must mean
    exactly one thing — **this machine has no serial ports** — because the person reading it is
    about to go and reinstall a USB driver on the strength of it. Two causes behind one silent
    answer is the failure this codebase is written against, and a diagnostic gets no exemption.

    Lives here rather than in the bring-up CLI because the running system needs it too: a
    sensor that cannot open its port is far more useful with the list of ports that do exist
    printed underneath it.
    """
    try:
        from serial.tools import list_ports  # noqa: PLC0415 — optional dependency
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise SerialLibraryMissing(
            "pyserial is not installed in this environment, so no serial port can be seen "
            "whether or not one exists. Install the hardware extra:\n"
            "    pip install -e '.[hardware]'\n"
            "or run ./start.sh --live once, which installs it."
        ) from exc

    return [
        f"{port.device}  {port.description}"
        for port in sorted(list_ports.comports(), key=lambda p: p.device)
    ]


@runtime_checkable
class ByteLink(Protocol):
    """A bidirectional byte pipe. Deliberately smaller than a serial port."""

    def open(self) -> None:
        """Acquire the link. Idempotent."""

    def close(self) -> None:
        """Release it. Idempotent."""

    def read(self, max_bytes: int) -> bytes:
        """Up to `max_bytes` of whatever has arrived; `b""` when nothing has.

        Must not block beyond the configured read timeout: this is called from the ingestion
        cycle. An empty result means *no bytes yet*, never *the link is gone* — a dead link
        raises, because a link that reports emptiness on failure is a sensor that goes quietly
        offline and shows an operator an empty sky (FM-010).
        """

    def write(self, data: bytes) -> None:
        """Send bytes to the sensor."""

    @property
    def is_open(self) -> bool: ...


class SerialByteLink:
    """A real serial port, opened through the USB-to-TTL adapter.

    Wiring, and the one way to destroy the sensor: the module is powered from 5 V, but its
    UART pins are 3.3 V logic. The USB-to-TTL adapter's logic-level switch must be on 3.3 V.
    On 5 V it will over-drive the sensor's UART pins. Software cannot detect this or protect
    against it, which is why it is written here as well as in the README.
    """

    __slots__ = ("_config", "_port")

    def __init__(self, config: UartLinkConfig) -> None:
        self._config = config
        self._port: object | None = None

    @property
    def is_open(self) -> bool:
        return self._port is not None

    def open(self) -> None:
        if self._port is not None:
            return
        try:
            import serial  # noqa: PLC0415 — optional dependency, imported at point of use
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            raise LinkError(
                "pyserial is required to talk to a physical sensor; install the hardware "
                "extra: pip install -e '.[hardware]'"
            ) from exc

        try:
            self._port = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baud,
                timeout=self._config.read_timeout_s,
                write_timeout=self._config.read_timeout_s,
            )
        except Exception as exc:  # serial.SerialException and OSError both occur here
            raise LinkError(f"could not open {self._config.port}: {exc}") from exc

    def close(self) -> None:
        port = self._port
        self._port = None
        if port is not None:
            try:
                port.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - closing a dead port is not an error
                pass

    def read(self, max_bytes: int) -> bytes:
        if self._port is None:
            raise LinkError("read from a closed link")
        try:
            waiting = int(getattr(self._port, "in_waiting", 0))
            # Read what is buffered, but always ask for at least one byte so the port's own
            # timeout governs the wait rather than this returning instantly in a busy loop.
            return self._port.read(max(1, min(waiting, max_bytes)))  # type: ignore[attr-defined]
        except Exception as exc:
            self.close()
            raise LinkError(f"read failed on {self._config.port}: {exc}") from exc

    def write(self, data: bytes) -> None:
        if self._port is None:
            raise LinkError("write to a closed link")
        try:
            self._port.write(data)  # type: ignore[attr-defined]
        except Exception as exc:
            self.close()
            raise LinkError(f"write failed on {self._config.port}: {exc}") from exc
