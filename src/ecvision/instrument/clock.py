"""Sampling clock: one place that defines what "capture time" means.

The edge pipeline has two different clocks and they must not be conflated:

* ``time.perf_counter_ns`` is monotonic. It is the right clock for intervals
  (latency, jitter, deadlines), because it cannot jump when NTP corrects the
  wall clock.
* ``time.time_ns`` is wall clock. It is the only clock that can be compared
  across devices, but it can jump.

``SamplingClock`` keeps an anchor pair and exposes both readings. A hardware
integration (GPS/PTP/NTP) replaces the anchor and sets ``source``/``synced``;
the rest of the pipeline does not change.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

ClockSource = Literal["system", "monotonic", "ntp", "ptp", "gps"]


@dataclass(frozen=True, slots=True)
class ClockReading:
    """One paired monotonic/wall-clock reading."""

    mono_ns: int
    wall_ns: int
    source: ClockSource
    offset_ms: float
    synced: bool
    boot_id: str

    @property
    def wall_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.wall_ns / 1_000_000_000, tz=timezone.utc)


class SamplingClock:
    """Pair monotonic and wall-clock time with an explicit quality state.

    ``offset_ms`` and ``drift_ppm`` exist so tests and demos can inject a bad
    clock. Production deployments should call :meth:`sync` from an NTP/PTP
    client; the pipeline then records ``clock.synced=false`` until that happens.
    """

    def __init__(
        self,
        *,
        source: ClockSource = "system",
        boot_id: str | None = None,
        offset_ms: float = 0.0,
        drift_ppm: float = 0.0,
        synced: bool | None = None,
        mono_ns_fn: Callable[[], int] = time.perf_counter_ns,
        wall_ns_fn: Callable[[], int] = time.time_ns,
    ) -> None:
        if not -1_000_000.0 <= offset_ms <= 1_000_000.0:
            raise ValueError("offset_ms is implausibly large")
        if not -10_000.0 <= drift_ppm <= 10_000.0:
            raise ValueError("drift_ppm must be within +/-10000")
        self._source = source
        self._boot_id = boot_id or uuid4().hex[:12]
        self._offset_ms = float(offset_ms)
        self._drift_ppm = float(drift_ppm)
        self._synced = (source in {"ntp", "ptp", "gps"}) if synced is None else synced
        self._mono_ns = mono_ns_fn
        self._wall_ns = wall_ns_fn
        self._anchor_mono = self._mono_ns()
        self._anchor_wall = self._wall_ns()
        self._last_mono: int | None = None

    @property
    def boot_id(self) -> str:
        return self._boot_id

    @property
    def source(self) -> ClockSource:
        return self._source

    @property
    def offset_ms(self) -> float:
        return self._offset_ms

    @property
    def synced(self) -> bool:
        return self._synced

    def sync(self, *, offset_ms: float, source: ClockSource = "ntp") -> None:
        """Apply a measured offset and mark the clock synchronized."""
        if not -1_000_000.0 <= offset_ms <= 1_000_000.0:
            raise ValueError("offset_ms is implausibly large")
        self._offset_ms = float(offset_ms)
        self._source = source
        self._synced = True

    def mark_unsynced(self) -> None:
        self._synced = False

    def read(self, mono_ns: int | None = None) -> ClockReading:
        mono = self._mono_ns() if mono_ns is None else int(mono_ns)
        elapsed_s = (mono - self._anchor_mono) / 1_000_000_000
        drift_s = elapsed_s * self._drift_ppm * 1e-6
        wall = self._anchor_wall + int((elapsed_s + drift_s) * 1_000_000_000)
        wall += int(self._offset_ms * 1_000_000)
        self._last_mono = mono
        return ClockReading(
            mono_ns=mono,
            wall_ns=wall,
            source=self._source,
            offset_ms=self._offset_ms,
            synced=self._synced,
            boot_id=self._boot_id,
        )

    def wall_for_mono(self, mono_ns: int) -> int:
        """Convert a previously captured monotonic stamp to wall time."""
        return self.read(mono_ns=mono_ns).wall_ns


def monotonic_ns() -> int:
    """Indirection used by tests that need to patch the process clock."""
    return time.perf_counter_ns()
