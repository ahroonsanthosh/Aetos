"""Raw frame logging: every byte the radar sent, with the time it arrived.

Required before any detection logic built on this unit is trusted, and it is listed first in
the first-run checklist for a reason. The protocol constants this integration decodes with are
unverified, so the first question about any surprising behaviour is "what did the wire actually
carry" — and that question is unanswerable after the fact unless the bytes were kept.

The format is deliberately dull: one line per record, ISO-8601 UTC timestamp, a kind, and hex.
Line-oriented text so that `grep`, `wc -l` and eyeballing all work on it during a bench session,
and hex rather than binary so that a suspicious frame can be pasted into a bug report or checked
against the vendor document by hand.

Sessions are separated by a header line carrying the mount tilt, because a log of returns
without the tilt that produced them is uninterpretable — the tilt determines which slice of sky
was being scanned, and one fixed tilt is one fixed slice.
"""

from __future__ import annotations

from pathlib import Path

from backend.common.clock import Clock


class RawFrameLog:
    """Append-only hex log of the serial stream.

    Opens the file on construction rather than per write: a bench session produces thousands of
    records, and reopening for each would make the logger's own cost visible in the poll loop it
    sits inside.

    Failures to write are swallowed after the first. Logging is diagnostic, and a full disk
    during a test run must not take the radar offline — but a silent failure would be worse than
    useless, so the first failure is recorded and surfaced through `last_error`.
    """

    __slots__ = ("_path", "_clock", "_handle", "_last_error", "_records")

    def __init__(self, path: Path | str, clock: Clock) -> None:
        self._path = Path(path)
        self._clock = clock
        self._handle = None
        self._last_error: str | None = None
        self._records = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def records_written(self) -> int:
        return self._records

    @property
    def last_error(self) -> str | None:
        """First write failure encountered, or None. Reported through the driver's status so a
        log that stopped recording halfway through a session is visible rather than assumed."""
        return self._last_error

    def open(self, *, model: str, port: str, baud_rate: int, mount_tilt_deg: float) -> None:
        """Begin a session, writing the header that makes the records that follow meaningful."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self._path.open("a", encoding="utf-8")
        except OSError as exc:
            self._handle = None
            self._last_error = f"could not open raw log {self._path}: {exc}"
            return

        self._write_line(
            f"# session model={model} port={port} baud={baud_rate} "
            f"mount_tilt_deg={mount_tilt_deg}"
        )

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except OSError:
            # Nothing useful remains to be done with a file that will not close, and raising
            # here would turn a diagnostic problem into a disconnect failure.
            return

    def record_frame(self, kind: str, payload: bytes) -> None:
        """Log one decoded frame."""
        self._write_line(f"{self._timestamp()} {kind} {payload.hex()}")

    def record_discard_gap(self, byte_count: int) -> None:
        """Log how many bytes were dropped during resynchronisation.

        A count rather than the bytes themselves: the decoder discards as it hunts for sync and
        the bytes are gone by the time it reports the total. The count is what the diagnosis
        actually needs — a trickle alongside decoding frames is ordinary resynchronisation,
        while a flood with no decodes means the link is not carrying this protocol at all, and
        those two are told apart by magnitude, not by content.
        """
        if byte_count > 0:
            self._write_line(f"{self._timestamp()} discarded {byte_count}")

    def record_command(self, direction: str, data: bytes) -> None:
        """Log a configuration command sent or its reply received."""
        self._write_line(f"{self._timestamp()} {direction} {data.hex()}")

    def _timestamp(self) -> str:
        return self._clock.now().isoformat()

    def _write_line(self, line: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(line + "\n")
            self._handle.flush()
            self._records += 1
        except OSError as exc:
            if self._last_error is None:
                self._last_error = f"raw log write to {self._path} failed: {exc}"
            self.close()
