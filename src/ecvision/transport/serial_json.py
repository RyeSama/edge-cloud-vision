"""Newline-delimited JSON from a small sensor board.

The board is intentionally dumb: it samples, timestamps with its own monotonic
microsecond counter, and prints one compact JSON object per line. It has no RTC
and no network stack, so the host is responsible for wall-clock time, sequence
gap detection, and validation.

Wire format (115200 8N1, UTF-8, one object per line):

    {"schema_version":"1.0","kind":"sensor","device_id":"esp32-01",
     "boot_id":"a1b2c3d4","seq":42,"t_us":12345678,"lux":512.3,
     "distance_mm":803.0,"trigger":false}

Blank lines and lines starting with ``#`` are ignored. Invalid lines are
reported by the caller and never silently coerced.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ecvision.contracts import SensorReading


class SerialParseError(ValueError):
    """A line is not a valid sensor reading."""


def parse_sensor_line(line: str) -> SensorReading:
    """Parse exactly one line. Raises ``SerialParseError`` on any problem."""
    text = line.strip()
    if not text:
        raise SerialParseError("empty line")
    if text.startswith("#"):
        raise SerialParseError("comment line")
    if len(text) > 2048:
        raise SerialParseError("line too long (max 2048 bytes)")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SerialParseError(f"invalid JSON at column {exc.colno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise SerialParseError("top-level JSON value must be an object")
    try:
        return SensorReading.model_validate(payload)
    except ValidationError as exc:
        raise SerialParseError(str(exc)) from exc


def iter_sensor_lines(
    lines: Iterable[str],
    *,
    on_error: Callable[[SerialParseError, str], None] | None = None,
) -> Iterator[SensorReading]:
    """Yield valid readings, skipping blank/comment lines.

    ``on_error(exc, raw_line)`` is called for malformed lines so a monitor can
    count them without stopping the stream. If no callback is supplied, bad
    lines are skipped.
    """
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            yield parse_sensor_line(raw)
        except SerialParseError as exc:
            if on_error is not None:
                on_error(exc, raw)


def read_replay(path: str | Path) -> Iterator[SensorReading]:
    """Replay a captured NDJSON file without hardware or pyserial."""
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        yield from iter_sensor_lines(handle)


def iter_serial(
    port: str,
    *,
    baud: int = 115200,
    timeout_s: float = 1.0,
) -> Iterator[str]:
    """Yield raw lines from a serial port. Requires the optional ``pyserial``."""
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError('pyserial is not installed. Run: pip install -e ".[serial]"') from exc
    with serial.Serial(port, baud, timeout=timeout_s) as handle:
        while True:
            yield handle.readline().decode("utf-8", errors="replace")


@dataclass
class SensorSequenceTracker:
    """Detect board reboots, duplicates, and missing sequence numbers."""

    boot_id: str | None = None
    last_seq: int | None = None
    gaps: int = 0
    duplicates: int = 0
    reboots: int = 0

    def observe(self, reading: SensorReading) -> int:
        """Return missing count (0 = in order, -1 = duplicate/out of order)."""
        if reading.boot_id != self.boot_id:
            if self.boot_id is not None:
                self.reboots += 1
            self.boot_id = reading.boot_id
            self.last_seq = reading.seq
            return 0
        if self.last_seq is None:
            self.last_seq = reading.seq
            return 0
        delta = reading.seq - self.last_seq
        self.last_seq = reading.seq
        if delta == 1:
            return 0
        if delta <= 0:
            self.duplicates += 1
            return -1
        missing = delta - 1
        self.gaps += missing
        return missing
