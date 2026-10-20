"""Edge-Mem contracts: strict, explicit, and safe to serialize across processes.

The data model intentionally has no raw-image field. Memory items are either a
real event, an aggregated summary, or a cold-archive index row; all three share
the same query surface so the retriever does not need tier-specific branches in
its callers.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, field_validator

from ecvision.contracts import StrictModel


class MemoryTier(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class TimeWindow(StrictModel):
    """A half-open UTC window ``[start, end)`` plus a human-readable label."""

    start: datetime
    end: datetime
    label: str = Field(min_length=1, max_length=200)
    source: Literal["explicit", "relative", "fallback"] = "relative"

    @field_validator("start", "end")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("time window must include a timezone")
        return value

    @field_validator("end")
    @classmethod
    def end_after_start(cls, value: datetime, info) -> datetime:
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("end must be after start")
        return value


class EventSummary(StrictModel):
    """Aggregated event representation kept in the warm tier."""

    summary_id: str = Field(min_length=1, max_length=200)
    device_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    bucket_start: datetime
    bucket_end: datetime
    count: int = Field(ge=1)
    first_ts: datetime
    last_ts: datetime
    avg_confidence: float = Field(ge=0.0, le=1.0)
    max_confidence: float = Field(ge=0.0, le=1.0)
    risk_counts: dict[str, int] = Field(default_factory=dict)
    action_counts: dict[str, int] = Field(default_factory=dict)
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("bucket_start", "bucket_end", "first_ts", "last_ts")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("summary timestamps must include a timezone")
        return value


class MemoryItem(StrictModel):
    """One retrieval candidate.

    ``text`` is what goes into the context window. Raw event JSON is not
    embedded here, so a query response cannot accidentally leak pixels.
    """

    event_id: str = Field(min_length=1, max_length=200)
    device_id: str = Field(min_length=1, max_length=64)
    ts: datetime
    label: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)
    tier: MemoryTier
    summary_count: int = Field(default=1, ge=1)
    risk_level: str | None = Field(default=None, max_length=16)
    action: str | None = Field(default=None, max_length=256)
    text: str = Field(min_length=1, max_length=2000)
    score: float = Field(default=0.0, ge=0.0)
    time_rank: int | None = Field(default=None, ge=1)
    semantic_rank: int | None = Field(default=None, ge=1)
    capture_time: datetime | None = None
    ingest_time: datetime | None = None
    late: bool = False

    @field_validator("ts", "capture_time", "ingest_time")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("item timestamps must include a timezone")
        return value


class VectorRecord(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    vector: list[float] = Field(min_length=1)
    text: str = Field(default="", max_length=2000)
    metadata: dict[str, str | int | float] = Field(default_factory=dict)


class MemoryQuery(StrictModel):
    question: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)
    token_budget: int = Field(default=4000, ge=128, le=8000)
    device_id: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=128)
    format: Literal["auto", "markdown", "json"] = "auto"
    now: datetime | None = None
    compress: bool = True

    @field_validator("now")
    @classmethod
    def aware_now(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("now must include a timezone")
        return value


class ContextBundle(StrictModel):
    format: Literal["markdown", "json"]
    text: str
    token_count: int = Field(ge=0)
    truncated: bool
    item_count: int = Field(ge=0)


class MemoryQueryResult(StrictModel):
    question: str
    window: TimeWindow
    items: list[MemoryItem] = Field(default_factory=list, max_length=50)
    context: ContextBundle
    degraded: bool
    warnings: list[str] = Field(default_factory=list, max_length=20)
    total_candidates: int = Field(ge=0)
    retrieval_ms: float = Field(ge=0.0)


class MemoryAnswer(StrictModel):
    question: str
    answer: str | None = Field(default=None, max_length=8000)
    context: ContextBundle
    mode: Literal["llm", "json_timeline", "degraded"]
    warnings: list[str] = Field(default_factory=list, max_length=20)


class CompressionReport(StrictModel):
    now: datetime
    warm_summaries: int = Field(ge=0)
    warm_events_removed: int = Field(ge=0)
    cold_archived: int = Field(ge=0)
    vectors_deleted: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list, max_length=20)


class MemoryStats(StrictModel):
    events: int = Field(ge=0)
    summaries: int = Field(ge=0)
    cold_events: int = Field(ge=0)
    vectors: int = Field(ge=0)
    vector_backend: str
    vector_healthy: bool
    last_vector_error: str | None = None
    late_events: int = Field(default=0, ge=0)
    watermark: datetime | None = None
    allowed_lateness_s: int = Field(default=300, ge=0)


class CompressRequest(StrictModel):
    now: datetime | None = None

    @field_validator("now")
    @classmethod
    def aware_now(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("now must include a timezone")
        return value
