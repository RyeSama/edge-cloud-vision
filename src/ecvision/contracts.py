"""Language-neutral event contract, implemented with Pydantic for Python.

The JSON Schema in ``contracts/event.schema.json`` is the source of truth for
non-Python implementations (C++/ESP32 later). This module mirrors it and adds
runtime validation at both ends of the MQTT link.

Design rule: an event never carries raw pixels. ``extra="forbid"`` makes that
rule enforceable instead of aspirational.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GateDecision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"


class GateReason(str, Enum):
    HIGH_CONFIDENCE = "high_confidence"
    AMBIGUOUS = "ambiguous"
    LOW_CONFIDENCE = "low_confidence"


class SourceInfo(StrictModel):
    width: int = Field(gt=0, le=7680)
    height: int = Field(gt=0, le=4320)
    fps: float = Field(gt=0, le=240)


class TopK(StrictModel):
    label: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)


class InferenceInfo(StrictModel):
    model: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)
    label_id: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    latency_ms: float = Field(ge=0.0)
    top_k: list[TopK] = Field(default_factory=list, max_length=5)


class ClockInfo(StrictModel):
    """Quality of the clock that produced ``sample``.

    ``synced`` is explicit because a system clock without NTP/PTP is not a
    measurement-grade time base. The field is not cosmetic: dashboards should
    count unsynced samples separately.
    """

    source: Literal["system", "monotonic", "ntp", "ptp", "gps"]
    boot_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    offset_ms: float = Field(ge=-1_000_000.0, le=1_000_000.0)
    synced: bool


class SampleInfo(StrictModel):
    """Timing metadata for one acquired frame.

    Monotonic stamps measure intervals; wall stamps make the event joinable to
    other instruments. Both are retained intentionally.
    """

    capture_mono_ns: int = Field(ge=0)
    capture_wall_ns: int = Field(ge=0)
    read_done_mono_ns: int = Field(ge=0)
    infer_start_mono_ns: int = Field(ge=0)
    infer_done_mono_ns: int = Field(ge=0)
    emit_wall_ns: int = Field(ge=0)
    publish_done_mono_ns: int | None = Field(default=None, ge=0)
    capture_dt_us: float | None = Field(default=None, ge=0.0)
    dropped_since_last: int = Field(default=0, ge=0)
    end_to_end_ms: float = Field(ge=0.0)
    deadline_ms: float = Field(gt=0.0)
    deadline_missed: bool = False
    clock: ClockInfo


class GateInfo(StrictModel):
    decision: GateDecision
    reason: GateReason
    accept_threshold: float = Field(ge=0.0, le=1.0)
    review_threshold: float = Field(ge=0.0, le=1.0)
    calibrated_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    calibration_version: str | None = Field(default=None, max_length=64)
    conformal_qhat: float | None = Field(default=None, ge=0.0, le=1.0)
    conformal_set: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("review_threshold")
    @classmethod
    def review_not_above_accept(cls, value: float, info) -> float:
        accept = info.data.get("accept_threshold")
        if accept is not None and value > accept:
            raise ValueError("review_threshold must be <= accept_threshold")
        return value


class Event(StrictModel):
    """A single edge observation.

    ``seq`` is monotonic per device and lets the cloud detect gaps/dedup after
    reconnects. ``event_id`` is globally unique for idempotent processing.
    """

    schema_version: Literal["1.0", "1.1"] = "1.1"
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    seq: int = Field(ge=0)
    ts: datetime = Field(default_factory=utc_now)
    source: SourceInfo
    inference: InferenceInfo
    gate: GateInfo
    sample: SampleInfo | None = None

    @field_validator("ts")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ts must include a timezone")
        return value


class Decision(StrictModel):
    """Cloud-side decision. ``source`` makes degradation observable."""

    event_id: str
    risk_level: Literal["low", "medium", "high", "unknown"]
    action: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=512)
    source: Literal["llm", "rule_fallback"]
    model: str
    latency_ms: float = Field(ge=0.0)
    created_at: datetime = Field(default_factory=utc_now)


class EventRecord(StrictModel):
    event: Event
    decision: Decision | None = None
    received_at: datetime = Field(default_factory=utc_now)


class SensorReading(StrictModel):
    """One line from a small sensor board over UART.

    The board has no RTC. ``t_us`` is a monotonic microsecond counter since
    boot; the host records arrival wall time separately. ``boot_id`` lets the
    host distinguish a reboot from a sequence gap. ``lux``/``distance_mm`` are
    optional because not every board carries every sensor.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["sensor"] = "sensor"
    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    boot_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.:-]+$")
    seq: int = Field(ge=0)
    t_us: int = Field(ge=0)
    lux: float | None = Field(default=None, ge=0.0, le=200_000.0)
    distance_mm: float | None = Field(default=None, ge=0.0, le=10_000.0)
    trigger: bool = False
